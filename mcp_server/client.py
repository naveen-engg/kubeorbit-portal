"""Thin async HTTP client for KubeOrbit's own REST API.

Every MCP tool goes through `KubeOrbitClient.get()`, which only ever issues
GET requests. There is deliberately no `post()`/`put()`/`delete()` on this
client at all -- not "unused", *absent* -- so a bug in tools.py can add a new
read-only tool by calling `.get(...)` with a new path, but cannot accidentally
wire up a write/mutating call, since the method to do so doesn't exist here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .config import get_settings

logger = logging.getLogger("kubeorbit.mcp")


class KubeOrbitAPIError(Exception):
    """KubeOrbit's REST API returned a non-2xx response."""

    def __init__(self, status_code: int, path: str, body: Any):
        self.status_code = status_code
        self.path = path
        self.body = body
        message = body.get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"GET {path} -> HTTP {status_code}: {message}")


class KubeOrbitClient:
    def __init__(self) -> None:
        settings = get_settings()
        headers = {"Accept": "application/json"}
        if settings.kubeorbit_api_key:
            headers["X-API-Key"] = settings.kubeorbit_api_key
        self._client = httpx.AsyncClient(
            base_url=settings.kubeorbit_base_url,
            headers=headers,
            timeout=settings.request_timeout_seconds,
        )

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        # Drop None values so optional filters (namespace, container, ...)
        # don't get serialized as the literal string "None" in the query string.
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        logger.debug("MCP tool call -> GET %s %s", path, clean_params)
        resp = await self._client.get(path, params=clean_params)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise KubeOrbitAPIError(resp.status_code, path, body)
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


_client: Optional[KubeOrbitClient] = None


def get_client() -> KubeOrbitClient:
    global _client
    if _client is None:
        _client = KubeOrbitClient()
    return _client
