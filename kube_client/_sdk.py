"""kubeconfig helpers and the in-process Python Kubernetes SDK fast path.

`cached_kubectl_list` (in `_fetch.py`) tries `_sync_sdk_list` first for every
plain `get`/`list` call -- no subprocess, no JSON round-trip through a CLI --
and only falls back to shelling out to kubectl for resource types the SDK
path here doesn't (yet) special-case.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
import kubernetes.client
import kubernetes.config
from kubernetes.client.exceptions import ApiException

from config import get_runtime_env
from ._cli import CLIError

logger = logging.getLogger("kubeorbit.kube_client")


def kubeconfig_path_for_cluster(cluster_id: str) -> Path:
    """Each cluster gets its own isolated kubeconfig file so context names from
    different clouds/providers can never collide, and so authenticating one
    cluster never disturbs another cluster's credentials."""
    return get_runtime_env().kubeconfig_home / f"{cluster_id}.yaml"


def read_current_context(kubeconfig_path: Path) -> str:
    try:
        with kubeconfig_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise CLIError(["kubeconfig"], None, f"kubeconfig not found at {kubeconfig_path}") from None
    ctx = data.get("current-context")
    if not ctx:
        raise CLIError(["kubeconfig"], None, f"kubeconfig at {kubeconfig_path} has no current-context")
    return ctx


_api_client_pool: Dict[str, kubernetes.client.ApiClient] = {}


def _get_or_create_sdk_client(context: str, kubeconfig_path: Path) -> kubernetes.client.ApiClient:
    pool_key = f"{context}:{kubeconfig_path.resolve()}"
    if pool_key in _api_client_pool:
        return _api_client_pool[pool_key]

    cfg_file = str(kubeconfig_path) if kubeconfig_path.exists() else None
    client = kubernetes.config.new_client_from_config(
        config_file=cfg_file,
        context=context,
    )
    _api_client_pool[pool_key] = client
    return client


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
        if r in ("nodes", "node", "no"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_node()
        elif r in ("namespaces", "namespace", "ns"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespace()
        elif r in ("pods", "pod", "po"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_pod(namespace=namespace) if namespace else api.list_pod_for_all_namespaces()
        elif r in ("deployments", "deployment", "deploy"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = api.list_namespaced_deployment(namespace=namespace) if namespace else api.list_deployment_for_all_namespaces()
        elif r in ("statefulsets", "statefulset", "sts"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = api.list_namespaced_stateful_set(namespace=namespace) if namespace else api.list_stateful_set_for_all_namespaces()
        elif r in ("daemonsets", "daemonset", "ds"):
            api = kubernetes.client.AppsV1Api(api_client=client)
            raw = api.list_namespaced_daemon_set(namespace=namespace) if namespace else api.list_daemon_set_for_all_namespaces()
        elif r in ("jobs", "job"):
            api = kubernetes.client.BatchV1Api(api_client=client)
            raw = api.list_namespaced_job(namespace=namespace) if namespace else api.list_job_for_all_namespaces()
        elif r in ("cronjobs", "cronjob", "cj"):
            api = kubernetes.client.BatchV1Api(api_client=client)
            raw = api.list_namespaced_cron_job(namespace=namespace) if namespace else api.list_cron_job_for_all_namespaces()
        elif r in ("services", "service", "svc"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_service(namespace=namespace) if namespace else api.list_service_for_all_namespaces()
        elif r in ("configmaps", "configmap", "cm"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_config_map(namespace=namespace) if namespace else api.list_config_map_for_all_namespaces()
        elif r in ("secrets", "secret"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_secret(namespace=namespace) if namespace else api.list_secret_for_all_namespaces()
        elif r in ("serviceaccounts", "serviceaccount", "sa"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_service_account(namespace=namespace) if namespace else api.list_service_account_for_all_namespaces()
        elif r in ("persistentvolumeclaims", "persistentvolumeclaim", "pvcs", "pvc"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_persistent_volume_claim(namespace=namespace) if namespace else api.list_persistent_volume_claim_for_all_namespaces()
        elif r in ("persistentvolumes", "persistentvolume", "pvs", "pv"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_persistent_volume()
        elif r in ("storageclasses", "storageclass", "sc"):
            api = kubernetes.client.StorageV1Api(api_client=client)
            raw = api.list_storage_class()
        elif r in ("events", "event", "ev"):
            api = kubernetes.client.CoreV1Api(api_client=client)
            raw = api.list_namespaced_event(namespace=namespace) if namespace else api.list_event_for_all_namespaces()
        elif r in ("ingresses", "ingress", "ing"):
            api = kubernetes.client.NetworkingV1Api(api_client=client)
            raw = api.list_namespaced_ingress(namespace=namespace) if namespace else api.list_ingress_for_all_namespaces()
        elif r in ("gateways", "gateway", "gtw"):
            api = kubernetes.client.CustomObjectsApi(api_client=client)
            if namespace:
                raw = api.list_namespaced_custom_object("gateway.networking.k8s.io", "v1", namespace, "gateways")
            else:
                raw = api.list_cluster_custom_object("gateway.networking.k8s.io", "v1", "gateways")
            return (raw or {}).get("items", []), True
        elif r in ("httproutes", "httproute"):
            api = kubernetes.client.CustomObjectsApi(api_client=client)
            if namespace:
                raw = api.list_namespaced_custom_object("gateway.networking.k8s.io", "v1", namespace, "httproutes")
            else:
                raw = api.list_cluster_custom_object("gateway.networking.k8s.io", "v1", "httproutes")
            return (raw or {}).get("items", []), True
        else:
            return None, False

        if raw is not None:
            sanitized = client.sanitize_for_serialization(raw)
            return (sanitized or {}).get("items", []), True
    except ApiException as e:
        if e.status == 404:
            return [], False
        logger.debug("SDK call for %s on %s returned HTTP %s, falling back to CLI", resource, context, e.status)
        return None, False
    except Exception as e:
        logger.debug("SDK call for %s on %s encountered error (%s), falling back to CLI", resource, context, e)
        return None, False

    return None, False
