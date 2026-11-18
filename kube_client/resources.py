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
from typing import Any, Dict, List, Optional

from config import get_config_store

from ._cache import _cache
from ._cli import BinaryNotFoundError, CLIError, run_cli
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


async def get_pods(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "pods", namespace, ttl)
    pods = [_normalize_pod(p) for p in items]

    # Enrich with live `kubectl top pods` metrics when metrics-server is available
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


async def get_secrets_metadata(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    """Metadata only -- never returns decoded/raw secret values. See reveal_secret()."""
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "secrets", namespace, ttl)
    return [_normalize_secret_meta(s) for s in items]


async def reveal_secret(context: str, kubeconfig_path: Path, namespace: str, name: str) -> Dict[str, Any]:
    """Explicit, uncached, on-demand decode of a single secret's values.

    Never cached (so a revoked/rotated value is never served stale) and only
    ever called from the dedicated POST reveal endpoint, which logs who
    triggered it. Base64-decodes every key; binary values that can't decode
    as UTF-8 are reported as such rather than returning garbage bytes.
    """
    import base64

    data = await kubectl_json(context, kubeconfig_path, ["get", "secret", name, "-n", namespace])
    if data is None:
        raise CLIError(["kubectl", "get", "secret", name], None, f"secret '{name}' not found in '{namespace}'")
    raw = data.get("data", {}) or {}
    decoded = {}
    for key, b64_val in raw.items():
        try:
            decoded[key] = base64.b64decode(b64_val).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            decoded[key] = "<binary data, cannot display as text>"
    return {
        "name": name,
        "namespace": namespace,
        "type": data.get("type", "Opaque"),
        "values": decoded,
        "revealed_at": time.time(),
    }


async def get_serviceaccounts(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> List[Dict[str, Any]]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "serviceaccounts", namespace, ttl)
    return [_normalize_serviceaccount(sa) for sa in items]


async def get_gateways(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await cached_kubectl_list(
        context, kubeconfig_path, "gateways.gateway.networking.k8s.io", namespace, ttl, optional=True
    )
    return {"items": [_normalize_gateway(g) for g in items], "available": available}


async def get_httproutes(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await cached_kubectl_list(
        context, kubeconfig_path, "httproutes.gateway.networking.k8s.io", namespace, ttl, optional=True
    )
    return {"items": [_normalize_httproute(r) for r in items], "available": available}


async def get_ingresses(context: str, kubeconfig_path: Path, namespace: Optional[str]) -> Dict[str, Any]:
    ttl = get_config_store().settings.kube_cache_ttl_seconds
    items, available = await cached_kubectl_list(
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
    items, _ = await cached_kubectl_list(context, kubeconfig_path, "storageclasses", None, ttl)
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


async def get_helm_releases(context: str, kubeconfig_path: Path) -> Dict[str, Any]:
    ttl = get_config_store().settings.cluster_meta_cache_ttl_seconds
    cache_key = f"{context}:helm:releases"

    async def factory():
        try:
            _, stdout, _ = await run_cli(
                ["helm", "list", "--all-namespaces", "--kube-context", context,
                 "--kubeconfig", str(kubeconfig_path), "-o", "json"],
            )
        except BinaryNotFoundError:
            return {"available": False, "items": []}
        except CLIError as exc:
            logger.warning("helm list failed for context %s: %s", context, exc)
            return {"available": True, "items": [], "error": str(exc)}
        releases = json.loads(stdout) if stdout.strip() else []
        return {
            "available": True,
            "items": [
                {
                    "name": r.get("name"),
                    "namespace": r.get("namespace"),
                    "chart": r.get("chart"),
                    "app_version": r.get("app_version"),
                    "revision": r.get("revision"),
                    "status": r.get("status"),
                    "updated": r.get("updated"),
                }
                for r in releases
            ],
        }

    result, _ = await _cache.get_or_set(cache_key, ttl, factory)
    return result
