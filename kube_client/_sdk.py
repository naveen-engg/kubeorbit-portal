"""kubeconfig helpers and the in-process Python Kubernetes SDK fast path.

`cached_kubectl_list` (in `_fetch.py`) tries `_sync_sdk_list` first for every
plain `get`/`list` call -- no subprocess, no JSON round-trip through a CLI --
and only falls back to shelling out to kubectl for resource types the SDK
path here doesn't (yet) special-case.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
import kubernetes.client
import kubernetes.config
from kubernetes.client.exceptions import ApiException

from config import get_runtime_env
from ._cli import CLIError, ResourceForbiddenError, ResourceTypeUnavailableError

logger = logging.getLogger("kubeorbit.kube_client")


def kubeconfig_path_for_cluster(cluster_id: str) -> Path:
    """Each cluster gets its own isolated kubeconfig file so context names from
    different clouds/providers can never collide, and so authenticating one
    cluster never disturbs another cluster's credentials."""
    return get_runtime_env().kubeconfig_home / f"{cluster_id}.yaml"


def read_current_context(kubeconfig_path: Path | str) -> str:
    p = Path(kubeconfig_path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise CLIError(["kubeconfig"], None, f"kubeconfig not found at {p}") from None
    ctx = data.get("current-context")
    if not ctx:
        raise CLIError(["kubeconfig"], None, f"kubeconfig at {p} has no current-context")
    return ctx


_MAX_API_CLIENTS = 32
_api_client_pool: OrderedDict[str, kubernetes.client.ApiClient] = OrderedDict()
_pool_lock = threading.Lock()
_pool_init_locks: Dict[str, threading.Lock] = {}


def _close_sdk_client_safely(client: kubernetes.client.ApiClient) -> None:
    try:
        client.close()
    except Exception:
        pass


def evict_sdk_clients_for_cluster(cluster_id: str) -> int:
    """Close and evict pooled Kubernetes SDK clients belonging to `cluster_id`."""
    suffix = f"{cluster_id}.yaml"
    removed = 0
    with _pool_lock:
        matching_keys = [
            key
            for key in list(_api_client_pool.keys())
            if key.endswith(f"/{suffix}") or key.endswith(f"\\{suffix}") or key.endswith(f":{suffix}")
        ]
        for key in matching_keys:
            old_client = _api_client_pool.pop(key, None)
            if old_client is not None:
                _close_sdk_client_safely(old_client)
                removed += 1
    return removed


def close_all_sdk_clients() -> None:
    """Close and drain every pooled Kubernetes SDK client."""
    with _pool_lock:
        while _api_client_pool:
            _, old_client = _api_client_pool.popitem(last=False)
            _close_sdk_client_safely(old_client)


def _get_or_create_sdk_client(context: str, kubeconfig_path: Path) -> kubernetes.client.ApiClient:
    pool_key = f"{context}:{kubeconfig_path.resolve()}"
    with _pool_lock:
        if pool_key in _api_client_pool:
            _api_client_pool.move_to_end(pool_key)
            return _api_client_pool[pool_key]
        init_lock = _pool_init_locks.setdefault(pool_key, threading.Lock())

    with init_lock:
        with _pool_lock:
            if pool_key in _api_client_pool:
                _api_client_pool.move_to_end(pool_key)
                return _api_client_pool[pool_key]

        cfg_file = str(kubeconfig_path) if kubeconfig_path.exists() else None
        cfg = kubernetes.client.Configuration()
        kubernetes.config.load_kube_config(
            config_file=cfg_file,
            context=context,
            client_configuration=cfg,
        )
        cfg.connection_pool_maxsize = 32
        client = kubernetes.client.ApiClient(configuration=cfg)

        with _pool_lock:
            if pool_key in _api_client_pool:
                _close_sdk_client_safely(client)
                _api_client_pool.move_to_end(pool_key)
                return _api_client_pool[pool_key]
            while len(_api_client_pool) >= _MAX_API_CLIENTS:
                _, old_client = _api_client_pool.popitem(last=False)
                _close_sdk_client_safely(old_client)
            _api_client_pool[pool_key] = client
            return client


_GATEWAY_API_GATEWAYS = ("gateways", "gateway", "gtw", "gateways.gateway.networking.k8s.io")
_GATEWAY_API_HTTPROUTES = ("httproutes", "httproute", "httproutes.gateway.networking.k8s.io")
_GATEWAY_API_RESOURCES = _GATEWAY_API_GATEWAYS + _GATEWAY_API_HTTPROUTES


def _sync_sdk_list(
    context: str,
    kubeconfig_path: Path,
    resource: str,
    namespace: Optional[str] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    """Fetch resources using the official Python Kubernetes SDK and serialize to dicts.
    Returns (items, is_handled). If is_handled is False, caller will fall back to CLI."""
    r = resource.lower().strip()
    try:
        client = _get_or_create_sdk_client(context, kubeconfig_path)
    except Exception as e:
        logger.debug("Failed to initialize SDK client for %s: %s", context, e)
        return None, False

    try:
        raw = None
        req_timeout = 8
        if r in ("nodes", "node", "no"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_node(_request_timeout=req_timeout)
        elif r in ("namespaces", "namespace", "ns"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespace(_request_timeout=req_timeout)
        elif r in ("pods", "pod", "po"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_pod(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_pod_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("deployments", "deployment", "deploy", "deployments.apps"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = (
                api.list_namespaced_deployment(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_deployment_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("statefulsets", "statefulset", "sts", "statefulsets.apps"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = (
                api.list_namespaced_stateful_set(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_stateful_set_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("daemonsets", "daemonset", "ds", "daemonsets.apps"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = (
                api.list_namespaced_daemon_set(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_daemon_set_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("jobs", "job", "jobs.batch"):
            api = kubernetes.client.BatchV1Api(api_client=client)
            raw = (
                api.list_namespaced_job(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_job_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("cronjobs", "cronjob", "cj", "cronjobs.batch"):
            api = kubernetes.client.BatchV1Api(api_client=client)
            raw = (
                api.list_namespaced_cron_job(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_cron_job_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("services", "service", "svc"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_service(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_service_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("configmaps", "configmap", "cm"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_config_map(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_config_map_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("secrets", "secret"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_secret(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_secret_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("serviceaccounts", "serviceaccount", "sa"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_service_account(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_service_account_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("persistentvolumeclaims", "persistentvolumeclaim", "pvcs", "pvc"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_persistent_volume_claim(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_persistent_volume_claim_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("persistentvolumes", "persistentvolume", "pvs", "pv"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_persistent_volume(_request_timeout=req_timeout)
        elif r in ("storageclasses", "storageclass", "sc", "storageclasses.storage.k8s.io"):
            api = kubernetes.client.StorageV1Api(api_client=client)
            raw = api.list_storage_class(_request_timeout=req_timeout)
        elif r in ("events", "event", "ev"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = (
                api.list_namespaced_event(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_event_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("ingresses", "ingress", "ing", "ingresses.networking.k8s.io"):
            api = kubernetes.client.NetworkingV1Api(api_client=client)
            raw = (
                api.list_namespaced_ingress(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_ingress_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("replicasets", "replicaset", "rs", "replicasets.apps"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = (
                api.list_namespaced_replica_set(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_replica_set_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("networkpolicies", "networkpolicy", "netpol", "networkpolicies.networking.k8s.io"):
            api = kubernetes.client.NetworkingV1Api(api_client=client)
            raw = (
                api.list_namespaced_network_policy(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_network_policy_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in ("horizontalpodautoscalers", "horizontalpodautoscaler", "hpa", "horizontalpodautoscalers.autoscaling"):
            api = kubernetes.client.AutoscalingV2Api(api_client=client)
            raw = (
                api.list_namespaced_horizontal_pod_autoscaler(namespace=namespace, _request_timeout=req_timeout)
                if namespace
                else api.list_horizontal_pod_autoscaler_for_all_namespaces(_request_timeout=req_timeout)
            )
        elif r in _GATEWAY_API_GATEWAYS:
            api = kubernetes.client.CustomObjectsApi(api_client=client)
            if namespace:
                raw = api.list_namespaced_custom_object(
                    "gateway.networking.k8s.io", "v1", namespace, "gateways", _request_timeout=req_timeout
                )
            else:
                raw = api.list_cluster_custom_object(
                    "gateway.networking.k8s.io", "v1", "gateways", _request_timeout=req_timeout
                )
            return (raw or {}).get("items", []), True
        elif r in _GATEWAY_API_HTTPROUTES:
            api = kubernetes.client.CustomObjectsApi(api_client=client)
            if namespace:
                raw = api.list_namespaced_custom_object(
                    "gateway.networking.k8s.io", "v1", namespace, "httproutes", _request_timeout=req_timeout
                )
            else:
                raw = api.list_cluster_custom_object(
                    "gateway.networking.k8s.io", "v1", "httproutes", _request_timeout=req_timeout
                )
            return (raw or {}).get("items", []), True
        else:
            return None, False

        if raw is not None:
            sanitized = client.sanitize_for_serialization(raw)
            return (sanitized or {}).get("items", []), True
    except ApiException as e:
        if e.status == 404:
            if r in _GATEWAY_API_RESOURCES:
                raise ResourceTypeUnavailableError(f"Resource type '{resource}' is not available on cluster") from e
            return None, False
        if e.status == 403:
            raise ResourceForbiddenError(["sdk", "list", resource], 403, str(e)) from e
        logger.debug("SDK list failed for %s (%s), falling back to CLI", resource, e)
        return None, False
    except Exception as e:
        logger.debug("SDK list failed for %s (%s), falling back to CLI", resource, e)
        return None, False

    return None, False


def _sync_sdk_list_crd_names(context: str, kubeconfig_path: Path) -> Optional[set[str]]:
    """Return installed CRD names (`<plural>.<group>`) via one lightweight API call,
    or None if CRD discovery is unavailable/forbidden so callers can fall back cleanly."""
    try:
        client = _get_or_create_sdk_client(context, kubeconfig_path)
        api = kubernetes.client.ApiextensionsV1Api(api_client=client)
        raw = api.list_custom_resource_definition(_request_timeout=8)
        items = getattr(raw, "items", None) or []
        return {
            item.metadata.name
            for item in items
            if getattr(item, "metadata", None) and getattr(item.metadata, "name", None)
        }
    except Exception as e:
        logger.debug("SDK CRD discovery failed for %s (%s), falling back to per-CRD checks", context, e)
        return None
