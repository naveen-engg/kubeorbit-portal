"""Tiny in-memory TTL cache shared by every CLI/SDK-backed read, so repeated
dashboard polling doesn't hammer kubectl/helm on every request."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Tuple


class TTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_set(self, key: str, ttl: int, factory):
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                expires_at, value = entry
                if expires_at > time.time():
                    return value, True
        value = await factory()
        async with self._lock:
            self._store[key] = (time.time() + ttl, value)
        return value, False

    def invalidate_prefix(self, prefix: str) -> None:
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


_cache = TTLCache()
_kubectl_semaphore = asyncio.Semaphore(8)  # cap concurrent shell-outs per process


async def cached(key: str, ttl: int, factory):
    """Shared TTL cache, exposed for callers outside this package (e.g. app.py
    caching provider.get_cluster_metadata(), which shells out to
    gcloud/aws/az) so every CLI-backed read goes through one cache."""
    value, _ = await _cache.get_or_set(key, ttl, factory)
    return value
