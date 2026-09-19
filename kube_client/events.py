"""Extended events: live cluster events merged into a per-cluster ring buffer
so short-lived events don't vanish the moment the API server GCs them."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .resources import get_events

_HISTORICAL_EVENTS: Dict[str, List[Dict[str, Any]]] = {}
_MAX_EVENT_HISTORY = 1000


async def get_extended_events(
    context: str, kubeconfig: Path, namespace: Optional[str] = None, limit: int = 200
) -> Dict[str, Any]:
    """Fetch live cluster events merged with historical ring buffer."""
    live_events = await get_events(context, kubeconfig, namespace, limit=limit)

    # Update ring buffer
    cluster_key = context or "default"
    history = _HISTORICAL_EVENTS.setdefault(cluster_key, [])

    seen_keys = {f"{e.get('namespace')}/{e.get('name')}/{e.get('last_seen')}" for e in history}
    for e in live_events:
        k = f"{e.get('namespace')}/{e.get('name')}/{e.get('last_seen')}"
        if k not in seen_keys:
            history.insert(0, e)
            seen_keys.add(k)

    if len(history) > _MAX_EVENT_HISTORY:
        del history[_MAX_EVENT_HISTORY:]

    filtered = history
    if namespace:
        filtered = [e for e in filtered if e.get("namespace") == namespace]

    # Compute event summary statistics
    warnings = [e for e in filtered if (e.get("type") or "").lower() == "warning"]
    normal = [e for e in filtered if (e.get("type") or "").lower() != "warning"]

    return {
        "items": filtered[:limit],
        "total_events": len(filtered),
        "warning_count": len(warnings),
        "normal_count": len(normal),
    }
