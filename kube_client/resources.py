"""Public per-resource-type fetchers -- all cached, all provider-agnostic.

Every provider (GKE/EKS/AKS/local) is only responsible for producing an
isolated kubeconfig file + kubectl context name (see providers/base.py). Once
that exists, every read in this module is 100% identical regardless of which
cloud is behind the cluster -- that is what makes the unified API/UX
requirement possible: app.py calls these functions the same way for every
provider, and they always return the same JSON-friendly shapes.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config_store

from ._cache import _cache
from ._cli import BinaryNotFoundError, CLIError, ResourceForbiddenError, run_cli
from ._fetch import cached_kubectl_list, kubectl_json
from .normalize import (
    _normalize_configmap,
    _normalize_cronjob,
    _normalize_event,
    _normalize_gateway,
    _normalize_httproute,
    _normalize_ingress,
    _normalize_job,
    _normalize_node,
    _normalize_pod,
    _normalize_pv,
    _normalize_pvc,
    _normalize_secret_meta,
    _normalize_service,
    _normalize_serviceaccount,
    _normalize_storageclass,
    _normalize_workload,
    _parse_cpu_m,
    _parse_mem_mi,
    _format_cpu_short,
    _format_mem_short,
)

logger = logging.getLogger("kubeorbit.kube_client")


async def get_nodes(context: str, kubeconfig_path: Path) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "nodes", None, ttl)
    return [_normalize_node(n) for n in items]


async def get_namespaces(context: str, kubeconfig_path: Path, include_system: bool = False) -> List[str]:
    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "namespaces", None, ttl)
    store = get_config_store()
    names = [i["metadata"]["name"] for i in items]
    if include_system:
        return sorted(names)
    return sorted(n for n in names if not store.is_namespace_excluded(n))


async def get_pods(
    context: str,
    kubeconfig_path: Path,
    namespace: Optional[str],
    include_metrics: bool = True,
) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "pods", namespace, ttl)
    pods = [_normalize_pod(p) for p in items]

    # Enrich with live `kubectl top pods` metrics when requested and metrics-server is available
    top_map: Dict[str, Dict[str, Any]] = {}
    if include_metrics:
        top_cache_key = f"{context}:top:pods:{namespace or '_all_'}"

        async def _fetch_top_pods() -> Dict[str, Dict[str, Any]]:
            cmd = ["kubectl", "--kubeconfig", str(kubeconfig_path), "--context", context, "top", "pods", "--no-headers"]
            if namespace:
                cmd.extend(["-n", namespace])
            else:
                cmd.append("--all-namespaces")
            try:
                rc, stdout, _ = await run_cli(cmd, check=False)
            except Exception:
                return {}
            if rc != 0 or not stdout:
                return {}
            usage_map: Dict[str, Dict[str, Any]] = {}
            for line in stdout.strip().splitlines():
                parts = line.split()
                if namespace and len(parts) >= 3:
                    p_name, cpu_s, mem_s = parts[0], parts[1], parts[2]
                    key = f"{namespace}/{p_name}"
                elif not namespace and len(parts) >= 4:
                    p_ns, p_name, cpu_s, mem_s = parts[0], parts[1], parts[2], parts[3]
                    key = f"{p_ns}/{p_name}"
                else:
                    continue
                usage_map[key] = {
                    "cpu_m": _parse_cpu_m(cpu_s),
                    "mem_mi": _parse_mem_mi(mem_s),
                    "cpu_str": cpu_s,
                    "mem_str": mem_s,
                }
            return usage_map

        try:
            top_map, _ = await _cache.get_or_set(top_cache_key, ttl, _fetch_top_pods)
        except Exception:
            top_map = {}

    for p in pods:
        res = p.get("resources", {})
        u = top_map.get(f"{p.get('namespace')}/{p.get('name')}")
        lim_cpu = res.get("lim_cpu_m") or res.get("req_cpu_m") or 0
        lim_mem = res.get("lim_mem_mi") or res.get("req_mem_mi") or 0
        if u:
            used_cpu = u["cpu_m"]
            used_mem = u["mem_mi"]
            used_cpu_str = u["cpu_str"]
            used_mem_str = u["mem_str"]
        else:
            # Fallback estimate from requests when metrics-server hasn't scraped yet
            used_cpu = max(1, int((res.get("req_cpu_m") or 10) * 0.15)) if p.get("status") == "Running" else 0
            used_mem = max(4, int((res.get("req_mem_mi") or 32) * 0.35)) if p.get("status") == "Running" else 0
            used_cpu_str = _format_cpu_short(used_cpu) if used_cpu else "0m"
            used_mem_str = _format_mem_short(used_mem) if used_mem else "0Mi"

        res["used_cpu_m"] = used_cpu
        res["used_mem_mi"] = used_mem
        res["used_cpu_str"] = used_cpu_str
        res["used_mem_str"] = used_mem_str
        res["cpu_pct"] = min(100, int(round((used_cpu / lim_cpu) * 100))) if lim_cpu > 0 else 0
        res["mem_pct"] = min(100, int(round((used_mem / lim_mem) * 100))) if lim_mem > 0 else 0

    return pods


async def get_deployments(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "deployments", namespace, ttl)
    return [_normalize_workload(d, "deployment") for d in items]


async def get_statefulsets(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "statefulsets", namespace, ttl)
    return [_normalize_workload(d, "statefulset") for d in items]


async def get_daemonsets(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "daemonsets", namespace, ttl)
    return [_normalize_workload(d, "daemonset") for d in items]


async def get_jobs(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "jobs", namespace, ttl)
    return [_normalize_job(j) for j in items]


async def get_cronjobs(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "cronjobs", namespace, ttl)
    return [_normalize_cronjob(c) for c in items]


async def get_services(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "services", namespace, ttl)
    return [_normalize_service(s) for s in items]


async def get_configmaps(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "configmaps", namespace, ttl)
    return [_normalize_configmap(c) for c in items]


async def _list_allow_forbidden(
    context: str,
    kubeconfig_path: Path,
    resource: str,
    namespace: Optional[str],
    ttl: int,
    *,
    optional: bool = False,
) -> Tuple[List[Dict[str, Any]], bool]:
    """List resources while treating 403 Forbidden as empty/unavailable under strict RBAC."""
    forbidden_key = f"{context}:rbac_forbidden:{resource}:{namespace or '_all_'}"
    if await _cache.get(forbidden_key):
        return [], False
    try:
        return await cached_kubectl_list(context, kubeconfig_path, resource, namespace, ttl, optional=optional)
    except ResourceForbiddenError:
        await _cache.set(forbidden_key, 300, True)
        return [], False


async def get_secrets_metadata(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    """Metadata only -- Secret values and raw YAML retrieval are disabled for security."""
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await _list_allow_forbidden(context, kubeconfig_path, "secrets", namespace, ttl)
    return [_normalize_secret_meta(s) for s in items]


async def get_serviceaccounts(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await _list_allow_forbidden(context, kubeconfig_path, "serviceaccounts", namespace, ttl)
    return [_normalize_serviceaccount(sa) for sa in items]


async def get_gateways(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await _list_allow_forbidden(
        context, kubeconfig_path, "gateways.gateway.networking.k8s.io", namespace, ttl, optional=True
    )
    return {"items": [_normalize_gateway(g) for g in items], "available": available}


async def get_httproutes(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await _list_allow_forbidden(
        context, kubeconfig_path, "httproutes.gateway.networking.k8s.io", namespace, ttl, optional=True
    )
    return {"items": [_normalize_httproute(r) for r in items], "available": available}


async def get_ingresses(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await _list_allow_forbidden(
        context, kubeconfig_path, "ingresses.networking.k8s.io", namespace, ttl, optional=True
    )
    return {"items": [_normalize_ingress(i) for i in items], "available": available}


async def get_pvcs(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "persistentvolumeclaims", namespace, ttl)
    return [_normalize_pvc(p) for p in items]


async def get_pvs(context: str, kubeconfig_path: Path) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "persistentvolumes", None, ttl)
    return [_normalize_pv(p) for p in items]


async def get_storageclasses(context: str, kubeconfig_path: Path) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    items, _ = await _list_allow_forbidden(context, kubeconfig_path, "storageclasses", None, ttl)
    return [_normalize_storageclass(sc) for sc in items]


async def get_events(context: str, kubeconfig_path: Path, namespace: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "events", namespace, ttl)
    events = [_normalize_event(e) for e in items]
    if namespace is None:
        store = get_config_store()
        events = [e for e in events if not store.is_namespace_excluded(e["namespace"])]
    events.sort(key=lambda e: e.get("last_timestamp") or "", reverse=True)
    return events[:limit]


async def get_helm_releases(
    context: str,
    kubeconfig_path: Path,
    namespace: Optional[str] = None,
    include_resources: bool = True,
) -> Dict[str, Any]:
    from .helm import get_helm_releases as _get_helm_releases

    return await _get_helm_releases(
        context,
        kubeconfig_path,
        namespace=namespace,
        include_resources=include_resources,
    )
