"""kubectl invocation and the cached SDK-first / CLI-fallback list helper
every resource fetcher in `resources.py`/`topology.py` is built on."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._cache import _cache, _kubectl_semaphore
from ._cli import ResourceTypeUnavailableError, run_cli
from ._sdk import _api_client_pool, _pool_lock, _sync_sdk_list


async def kubectl_json(
    context: str,
    kubeconfig_path: Path,
    args: List[str],
    check: bool = True,
) -> Optional[Any]:
    cmd = ["kubectl", "--kubeconfig", str(kubeconfig_path), "--context", context, *args, "-o", "json"]
    async with _kubectl_semaphore:
        _, stdout, _ = await run_cli(cmd, check=check)
    if not stdout.strip():
        return None
    return json.loads(stdout)


async def cached_kubectl_list(
    context: str,
    kubeconfig_path: Path,
    resource: str,
    namespace: Optional[str],
    ttl: int,
    extra_args: Optional[List[str]] = None,
    optional: bool = False,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Returns (items, available). `available=False` means the resource type
    doesn't exist on this cluster (only meaningful when optional=True)."""
    ns_key = namespace or "_all_"
    kcfg_key = kubeconfig_path.stem if kubeconfig_path else "default"
    cache_key = f"{context}:{kcfg_key}:{resource}:{ns_key}:{','.join(extra_args or [])}"

    async def factory():
        # 1. Fast-path: Try in-process Python Kubernetes client library (with 10s timeout)
        if not extra_args:
            try:
                items, is_handled = await asyncio.wait_for(
                    asyncio.to_thread(_sync_sdk_list, context, kubeconfig_path, resource, namespace),
                    timeout=10.0,
                )
                if is_handled and items is not None:
                    return {"items": items, "available": True}
            except asyncio.TimeoutError:
                pass

        # 2. Fallback: CLI execution
        args = ["get", resource]
        args += ["-n", namespace] if namespace else ["--all-namespaces"]
        if extra_args:
            args += extra_args
        try:
            data = await kubectl_json(context, kubeconfig_path, args)
        except ResourceTypeUnavailableError:
            if optional:
                return {"items": [], "available": False}
            raise
        return {"items": (data or {}).get("items", []), "available": True}

    result, _ = await _cache.get_or_set(cache_key, ttl, factory)
    return result["items"], result["available"]


def invalidate_cluster_cache(context: str) -> None:
    _cache.invalidate_prefix(f"{context}:")
    with _pool_lock:
        for k in list(_api_client_pool.keys()):
            if k.startswith(f"{context}:") or f":{context}:" in k:
                client = _api_client_pool.pop(k, None)
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
