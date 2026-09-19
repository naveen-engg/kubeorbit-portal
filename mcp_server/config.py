"""Runtime settings for the MCP server, sourced from environment variables.

Kept separate from the main app's config.py on purpose: this process talks
to KubeOrbit as an HTTP *client* (see client.py) and has its own, much
smaller surface -- a base URL, an optional API key, and transport settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class MCPSettings:
    # Where the KubeOrbit FastAPI app is reachable. This server never touches
    # kubectl/cloud CLIs itself -- every tool call is a plain HTTP GET against
    # this base URL, so it inherits KubeOrbit's own auth mode, caching, and
    # audit logging for free instead of re-implementing any of it.
    kubeorbit_base_url: str = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_BASE_URL", "http://localhost:8080")
    )
    # Sent as X-API-Key when KubeOrbit's own KUBEORBIT_AUTH_MODE=api_key.
    # Leave unset for KUBEORBIT_AUTH_MODE=none (local dev only, same caveat as
    # the main app: never do this in production).
    kubeorbit_api_key: str | None = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_API_KEY")
    )
    request_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("KUBEORBIT_MCP_TIMEOUT_SECONDS", "30"))
    )
    # "stdio" for a locally-spawned agent process (e.g. ADK's
    # StdioServerParameters during development); "streamable-http" to run
    # this as its own standing service an agent connects to over the network.
    transport: str = field(
        default_factory=lambda: os.environ.get("KUBEORBIT_MCP_TRANSPORT", "stdio")
    )
    host: str = field(default_factory=lambda: os.environ.get("KUBEORBIT_MCP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("KUBEORBIT_MCP_PORT", "8090")))


@lru_cache(maxsize=1)
def get_settings() -> MCPSettings:
    return MCPSettings()
