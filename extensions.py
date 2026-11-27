"""
Minimal Apache-2.0 strictly read-only extension loader for KubeOrbit Portal.

In pure OSS deployments (`naveen-engg/kubeorbit-portal`), `enterprise.plugin` is
not present and `load_extensions(app)` cleanly returns False with zero external
dependencies. In Enterprise deployments (`naveen-engg/kubeorbit-enterprise`),
`enterprise/plugin.py` sits alongside `app/` on `PYTHONPATH` without modifying
core OSS files, preserving conflict-free upstream syncs.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Callable, Dict, List

logger = logging.getLogger("kubeorbit.extensions")

_ui_modules: List[Dict[str, str]] = []
_mcp_tools: List[tuple[Callable[..., Any], Any]] = []
_loaded_modules: set[str] = set()


def register_ui_module(
    module_id: str,
    label: str,
    script_url: str,
    min_role: str = "viewer",
) -> None:
    """Register an optional read-only UI script module rendered in the sidebar."""
    for existing in _ui_modules:
        if existing["id"] == module_id:
            existing.update({"label": label, "script_url": script_url, "min_role": min_role})
            return
    _ui_modules.append(
        {
            "id": module_id,
            "label": label,
            "script_url": script_url,
            "min_role": min_role,
        }
    )


def list_ui_modules() -> List[Dict[str, str]]:
    return [dict(m) for m in _ui_modules]


def register_mcp_tool(fn: Callable[..., Any], spec: Any) -> None:
    """Track an extension-provided strictly read-only MCP tool."""
    for idx, (existing_fn, _) in enumerate(_mcp_tools):
        if existing_fn.__name__ == fn.__name__:
            _mcp_tools[idx] = (fn, spec)
            return
    _mcp_tools.append((fn, spec))


def list_extension_mcp_tools() -> List[tuple[Callable[..., Any], Any]]:
    return list(_mcp_tools)


def is_enterprise_loaded() -> bool:
    return bool(_loaded_modules)


def reset_extensions_for_testing() -> None:
    """Clear registered extension hooks (intended strictly for test isolation)."""
    _ui_modules.clear()
    _mcp_tools.clear()
    _loaded_modules.clear()


def load_extensions(app: Any) -> bool:
    """Import and initialize `enterprise.plugin` if present on PYTHONPATH.

    Must be invoked BEFORE mounting the root catch-all `/` static handler so
    extension API routes and `/enterprise-static` take precedence cleanly.
    """
    if os.environ.get("KUBEORBIT_ENTERPRISE_ENABLED", "true").strip().lower() == "false":
        return False
    mod_name = os.environ.get("KUBEORBIT_EXTENSION_MODULE", "enterprise.plugin").strip()
    if not mod_name:
        return False
    if mod_name in _loaded_modules:
        return True
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        top_pkg = mod_name.split(".", 1)[0]
        if exc.name in (mod_name, top_pkg):
            return False
        raise
    register_fn = getattr(mod, "register", None)
    if callable(register_fn):
        register_fn(app)
        _loaded_modules.add(mod_name)
        logger.info("Loaded KubeOrbit extension module '%s'", mod_name)
        return True
    return False
