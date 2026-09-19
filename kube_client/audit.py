"""In-memory audit trail for privileged user actions (secret reveal, rollback, ...)."""
from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kubeorbit.kube_client")

_AUDIT_LOGS: List[Dict[str, Any]] = []
_MAX_AUDIT_LOGS = 500


def record_audit_event(
    user: str,
    action: str,
    cluster_id: str,
    namespace: Optional[str],
    resource: str,
    details: str = "",
    status: str = "SUCCESS",
) -> Dict[str, Any]:
    """Record a privileged user action to the in-memory audit trail."""
    entry = {
        "id": f"audit-{int(time.time()*1000)}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "user": user or "anonymous",
        "action": action,
        "cluster_id": cluster_id,
        "namespace": namespace or "cluster-wide",
        "resource": resource,
        "details": details,
        "status": status,
    }
    _AUDIT_LOGS.insert(0, entry)
    if len(_AUDIT_LOGS) > _MAX_AUDIT_LOGS:
        _AUDIT_LOGS.pop()
    logger.info("AUDIT: [%s] %s by %s on %s/%s (%s)", status, action, user, cluster_id, resource, details)
    return entry


def get_audit_logs(cluster_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    if cluster_id:
        logs = [l for l in _AUDIT_LOGS if l["cluster_id"] == cluster_id]
    else:
        logs = _AUDIT_LOGS
    return logs[:limit]
