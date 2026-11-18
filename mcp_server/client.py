"""Thin async HTTP client for KubeOrbit's own REST API.

Every MCP tool goes through `KubeOrbitClient.get()`, which only ever issues
GET requests. There is deliberately no `post()`/`put()`/`delete()` on this
client at all -- not "unused", *absent* -- so a bug in tools.py can add a new
read-only tool by calling `.get(...)` with a new path, but cannot accidentally
wire up a write/mutating call, since the method to do so doesn't exist here.

When called in-process by `ai_agent.py`, `set_inprocess_user()` sets a
ContextVar with the current authenticated user's email/subject so tool requests
inherit the caller's identity (preventing HTTP 401 errors when KubeOrbit runs
behind Cloud Run IAP).
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Dict, Optional

import httpx

from .config import get_settings

logger = logging.getLogger("kubeorbit.mcp")

_current_user_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "kubeorbit_mcp_current_user", default=None
)
_inprocess_asgi_app: Any = None


def configure_inprocess_app(asgi_app: Any) -> None:
    """Register the FastAPI ASGI app for zero-hop in-process tool execution."""
    global _inprocess_asgi_app, _client
    if _inprocess_asgi_app is not asgi_app:
        _inprocess_asgi_app = asgi_app
        _client = None


def set_inprocess_user(user_subject: Optional[str]) -> contextvars.Token:
    """Bind the active portal user's subject/email for the current async task."""
    return _current_user_ctx.set(user_subject)


def reset_inprocess_user(token: contextvars.Token) -> None:
    _current_user_ctx.reset(token)


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

        transport = (
            httpx.ASGITransport(app=_inprocess_asgi_app)
            if _inprocess_asgi_app is not None
            else None
        )
        base_url = "http://kubeorbit.internal" if _inprocess_asgi_app is not None else settings.kubeorbit_base_url

        self._client = httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            headers=headers,
            timeout=settings.request_timeout_seconds,
        )

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        # Drop None values so optional filters (namespace, container, ...)
        # don't get serialized as the literal string "None" in the query string.
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        req_headers: Dict[str, str] = {}
        active_user = _current_user_ctx.get()
        if active_user:
            req_headers["X-Goog-Authenticated-User-Email"] = f"accounts.google.com:{active_user}"
            req_headers["X-MS-CLIENT-PRINCIPAL-NAME"] = active_user

        logger.debug("MCP tool call -> GET %s %s (user=%s)", path, clean_params, active_user or "default")
        resp = await self._client.get(path, params=clean_params, headers=req_headers)
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
