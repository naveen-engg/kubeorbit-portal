"""Tiny in-memory TTL cache shared by every CLI/SDK-backed read, so repeated
dashboard polling doesn't hammer kubectl/helm on every request.

Includes per-key singleflight Future deduplication so 10+ concurrent requests
on a cold or newly-expired key share a single underlying `factory()` execution
instead of triggering a cache stampede.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Tuple


class TTLCache:
    def __init__(self, max_entries: int = 1024):
        self._store: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._max_entries = max(1, max_entries)

    def _prune_locked(self, now_ts: float) -> None:
        expired_keys = [k for k, (expires_at, _) in self._store.items() if expires_at <= now_ts]
        for k in expired_keys:
            self._store.pop(k, None)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, ttl: int, factory):
        now_ts = time.time()
        is_leader = False
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                expires_at, value = entry
                if expires_at > now_ts:
                    self._store.move_to_end(key)
                    return value, True
                self._store.pop(key, None)
            fut = self._inflight.get(key)
            if fut is None:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._inflight[key] = fut
                is_leader = True

        if not is_leader:
            val = await asyncio.shield(fut)
            return val, True

        try:
            value = await factory()
        except Exception as exc:
            async with self._lock:
                self._inflight.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()  # mark retrieved so asyncio doesn't log "Future exception was never retrieved" if no followers
            raise

        now_write = time.time()
        async with self._lock:
            self._store[key] = (now_write + ttl, value)
            self._store.move_to_end(key)
            self._prune_locked(now_write)
            self._inflight.pop(key, None)
        if not fut.done():
            fut.set_result(value)
        return value, False

    async def get(self, key: str, default: Any = None) -> Any:
        now_ts = time.time()
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            expires_at, value = entry
            if expires_at <= now_ts:
                self._store.pop(key, None)
                return default
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, ttl: int, value: Any) -> None:
        now_write = time.time()
        async with self._lock:
            self._store[key] = (now_write + ttl, value)
            self._store.move_to_end(key)
            self._prune_locked(now_write)

    def invalidate_prefix(self, prefix: str) -> None:
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
        self._inflight.clear()


_cache = TTLCache()
_kubectl_semaphore = asyncio.Semaphore(8)  # cap concurrent shell-outs per process


async def cached(key: str, ttl: int, factory):
    """Shared TTL cache, exposed for callers outside this package (e.g. app.py
    caching provider.get_cluster_metadata(), which shells out to
    gcloud/aws/az) so every CLI-backed read goes through one cache."""
    value, _ = await _cache.get_or_set(key, ttl, factory)
    return value
