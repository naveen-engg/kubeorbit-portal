"""Workload Change Timeline aggregator (strictly read-only, zero Secret values).

Combines chronological configuration and runtime history for a Deployment,
StatefulSet, or DaemonSet into a single newest-first timeline:
1. ReplicaSet / workload rollout revisions (container image diffs & change-cause)
2. Helm release revisions (when workload carries Helm release metadata)
3. Referenced ConfigMap & Secret metadata (`resourceVersion` + timestamp; Secrets
   never expose `.data` or `.stringData`)
4. Live + historical Kubernetes Events matching the workload and its pods
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config_store

from ._fetch import cached_kubectl_list
from .events import get_extended_events
from .helm import get_helm_release_history
from .resources import get_secrets_metadata
from .workloads import get_rollout_history

_WORKLOAD_KIND_MAP: Dict[str, Tuple[str, str]] = {
    "deployment": ("Deployment", "deployments"),
    "deployments": ("Deployment", "deployments"),
    "statefulset": ("StatefulSet", "statefulsets"),
    "statefulsets": ("StatefulSet", "statefulsets"),
    "daemonset": ("DaemonSet", "daemonsets"),
    "daemonsets": ("DaemonSet", "daemonsets"),
}


def normalize_workload_kind(workload_type: str) -> Tuple[str, str]:
    """Return `(canonical_kind, plural_resource)` or raise `ValueError`."""
    key = (workload_type or "").strip().lower()
    if key not in _WORKLOAD_KIND_MAP:
        raise ValueError(
            f"Unsupported workload_type '{workload_type}'. Expected Deployment, StatefulSet, or DaemonSet."
        )
    return _WORKLOAD_KIND_MAP[key]


def extract_referenced_config_objects(workload_obj: Optional[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Extract sorted unique ConfigMap and Secret names referenced by a workload PodTemplate."""
    if not workload_obj:
        return [], []
    pod_spec = ((workload_obj.get("spec") or {}).get("template") or {}).get("spec") or {}

    cm_names = set()
    secret_names = set()

    for container in [*(pod_spec.get("containers") or []), *(pod_spec.get("initContainers") or [])]:
        for env_from in container.get("envFrom") or []:
            cm_ref = (env_from.get("configMapRef") or {}).get("name")
            if cm_ref:
                cm_names.add(str(cm_ref))
            sec_ref = (env_from.get("secretRef") or {}).get("name")
            if sec_ref:
                secret_names.add(str(sec_ref))

        for env in container.get("env") or []:
            val_from = env.get("valueFrom") or {}
            cm_key_ref = (val_from.get("configMapKeyRef") or {}).get("name")
            if cm_key_ref:
                cm_names.add(str(cm_key_ref))
            sec_key_ref = (val_from.get("secretKeyRef") or {}).get("name")
            if sec_key_ref:
                secret_names.add(str(sec_key_ref))

    for vol in pod_spec.get("volumes") or []:
        cm_vol = (vol.get("configMap") or {}).get("name")
        if cm_vol:
            cm_names.add(str(cm_vol))
        sec_vol = (vol.get("secret") or {}).get("secretName")
        if sec_vol:
            secret_names.add(str(sec_vol))

    return sorted(cm_names), sorted(secret_names)


def _is_owned_by_workload(obj: Dict[str, Any], kind: str, name: str) -> bool:
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if (ref.get("kind") or "").lower() == kind.lower() and ref.get("name") == name:
            return True
    return False


def _iso_sort_key(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    raw = str(ts).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _container_images(pod_template: Dict[str, Any]) -> List[str]:
    spec = (pod_template or {}).get("spec") or {}
    return [
        f"{c.get('name', 'container')}={c.get('image', '')}"
        for c in (spec.get("containers") or [])
        if c.get("image")
    ]


def build_workload_timeline(
    *,
    workload_type: str,
    name: str,
    namespace: str,
    workload_obj: Optional[Dict[str, Any]] = None,
    replicasets: Optional[List[Dict[str, Any]]] = None,
    rollout_revisions: Optional[List[Dict[str, Any]]] = None,
    helm_history: Optional[Dict[str, Any]] = None,
    configmaps: Optional[List[Dict[str, Any]]] = None,
    secrets_meta: Optional[List[Dict[str, Any]]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Pure builder merging rollout, Helm, ConfigMap/Secret metadata, and Event entries newest-first."""
    canonical_kind, _ = normalize_workload_kind(workload_type)
    items: List[Dict[str, Any]] = []

    # 1. Rollout / ReplicaSet revisions with image diffs
    owned_rs = [rs for rs in (replicasets or []) if _is_owned_by_workload(rs, canonical_kind, name)]
    rs_entries: List[Tuple[int, str, List[str], str, str, bool]] = []
    for rs in owned_rs:
        meta = rs.get("metadata") or {}
        anns = meta.get("annotations") or {}
        rev_raw = anns.get("deployment.kubernetes.io/revision") or "0"
        rev_num = int(rev_raw) if str(rev_raw).isdigit() else 0
        imgs = _container_images((rs.get("spec") or {}).get("template") or {})
        cause = anns.get("kubernetes.io/change-cause") or ""
        ts = meta.get("creationTimestamp") or ""
        spec_reps = int((rs.get("spec") or {}).get("replicas") or 0)
        ready_reps = int((rs.get("status") or {}).get("readyReplicas") or 0)
        rs_entries.append((rev_num, ts, imgs, cause, meta.get("name") or "", spec_reps > 0 or ready_reps > 0))

    rs_entries.sort(key=lambda r: (r[0], _iso_sort_key(r[1])))
    prev_imgs: Optional[List[str]] = None
    seen_revs = set()
    for rev_num, ts, imgs, cause, rs_name, is_active in rs_entries:
        seen_revs.add(rev_num)
        diff_text = ", ".join(imgs) if imgs else "no containers"
        if prev_imgs is not None and imgs != prev_imgs:
            diff_text = f"{', '.join(prev_imgs)} -> {', '.join(imgs)}"
        prev_imgs = imgs
        items.append({
            "kind": "rollout",
            "timestamp": ts,
            "severity": "info",
            "resource_kind": "ReplicaSet",
            "resource_name": rs_name or name,
            "title": f"Rollout Revision {rev_num}" + (" (active)" if is_active else ""),
            "detail": f"Images: {diff_text}" + (f" · Cause: {cause}" if cause else ""),
            "metadata": {
                "revision": rev_num,
                "images": imgs,
                "change_cause": cause,
                "is_active": is_active,
            },
        })

    # Merge rollout_history fallback revisions not already captured via ReplicaSets
    for rev in rollout_revisions or []:
        rev_num = int(rev.get("revision") or 0)
        if rev_num in seen_revs:
            continue
        cause = rev.get("change_cause") or ""
        img = rev.get("image") or ""
        detail_parts = []
        if img:
            detail_parts.append(f"Image: {img}")
        if cause and cause != "<none>":
            detail_parts.append(f"Cause: {cause}")
        items.append({
            "kind": "rollout",
            "timestamp": "",
            "severity": "info",
            "resource_kind": canonical_kind,
            "resource_name": name,
            "title": f"Rollout Revision {rev_num}",
            "detail": " · ".join(detail_parts) or "Revision recorded by kubectl rollout history",
            "metadata": {"revision": rev_num, "image": img, "change_cause": cause},
        })

    # 2. Helm revisions if workload belongs to a Helm release
    if helm_history and (helm_history.get("items") or []):
        rel_name = helm_history.get("release") or "release"
        for h_item in helm_history.get("items") or []:
            rev = h_item.get("revision")
            status = h_item.get("status") or ""
            chart = h_item.get("chart") or ""
            app_ver = h_item.get("app_version") or ""
            desc = h_item.get("description") or ""
            items.append({
                "kind": "helm",
                "timestamp": h_item.get("updated") or "",
                "severity": "warning" if "fail" in status.lower() else "info",
                "resource_kind": "HelmRelease",
                "resource_name": rel_name,
                "title": f"Helm Revision {rev} ({status})" if status else f"Helm Revision {rev}",
                "detail": " · ".join(part for part in [chart, f"app={app_ver}" if app_ver else "", desc] if part),
                "metadata": {
                    "revision": rev,
                    "chart": chart,
                    "app_version": app_ver,
                    "status": status,
                    "is_current": bool(h_item.get("is_current")),
                },
            })

    # 3. Referenced ConfigMap & Secret metadata (NEVER read Secret values)
    cm_refs, secret_refs = extract_referenced_config_objects(workload_obj)
    cm_by_name = {(cm.get("metadata") or {}).get("name"): cm for cm in (configmaps or [])}
    sec_by_name = {s.get("name"): s for s in (secrets_meta or [])}

    for cm_name in cm_refs:
        cm = cm_by_name.get(cm_name)
        if not cm:
            continue
        meta = cm.get("metadata") or {}
        rv = meta.get("resourceVersion") or "?"
        key_count = len(cm.get("data") or {}) + len(cm.get("binaryData") or {})
        items.append({
            "kind": "config",
            "timestamp": meta.get("creationTimestamp") or "",
            "severity": "info",
            "resource_kind": "ConfigMap",
            "resource_name": cm_name,
            "title": f"ConfigMap '{cm_name}' (resourceVersion {rv})",
            "detail": f"{key_count} key(s) mounted or referenced by {canonical_kind}/{name}.",
            "metadata": {"resource_version": rv, "key_count": key_count},
        })

    for sec_name in secret_refs:
        sec = sec_by_name.get(sec_name)
        if sec:
            rv = sec.get("resource_version") or "?"
            items.append({
                "kind": "config",
                "timestamp": sec.get("created_at") or "",
                "severity": "info",
                "resource_kind": "Secret",
                "resource_name": sec_name,
                "title": f"Secret '{sec_name}' (resourceVersion {rv}, metadata only)",
                "detail": "Referenced by workload; Secret keys/values omitted server-side for security.",
                "metadata": {"resource_version": rv, "masked_server_side": True},
            })
        else:
            items.append({
                "kind": "config",
                "timestamp": "",
                "severity": "info",
                "resource_kind": "Secret",
                "resource_name": sec_name,
                "title": f"Secret '{sec_name}' (referenced; RBAC metadata restricted)",
                "detail": "Referenced by workload podSpec; Secret object access omitted under strict read-only RBAC.",
                "metadata": {"resource_version": None, "masked_server_side": True, "rbac_restricted": True},
            })

    # 4. Kubernetes runtime events tied to workload, ReplicaSets, or Pods
    rs_names = {entry[4] for entry in rs_entries if entry[4]}
    prefix = f"{name}-"
    for ev in events or []:
        ev_ns = ev.get("namespace")
        if ev_ns and ev_ns != namespace:
            continue
        inv_name = ev.get("involved_name") or ""
        msg = ev.get("message") or ""
        if not (inv_name == name or inv_name in rs_names or inv_name.startswith(prefix) or name in msg):
            continue
        ev_type = ev.get("type") or "Normal"
        is_warn = ev_type.lower() == "warning"
        ts = ev.get("last_seen") or ev.get("last_timestamp") or ev.get("first_seen") or ""
        items.append({
            "kind": "event",
            "timestamp": ts,
            "severity": "warning" if is_warn else "info",
            "resource_kind": ev.get("involved_kind") or "Event",
            "resource_name": inv_name or name,
            "title": f"{ev_type}: {ev.get('reason') or 'Event'}",
            "detail": msg,
            "metadata": {"count": ev.get("count", 1), "reason": ev.get("reason") or ""},
        })

    items.sort(key=lambda entry: _iso_sort_key(entry.get("timestamp")), reverse=True)
    capped = items[: max(1, min(int(limit), 200))]

    return {
        "workload_type": canonical_kind,
        "name": name,
        "namespace": namespace,
        "count": len(capped),
        "summary": {
            "rollouts": sum(1 for e in capped if e["kind"] == "rollout"),
            "helm_revisions": sum(1 for e in capped if e["kind"] == "helm"),
            "config_refs": sum(1 for e in capped if e["kind"] == "config"),
            "events": sum(1 for e in capped if e["kind"] == "event"),
            "warnings": sum(1 for e in capped if e["severity"] == "warning"),
        },
        "items": capped,
    }


async def _safe_rollout_history(
    context: str, kubeconfig_path: Path, namespace: str, kind_lower: str, name: str
) -> List[Dict[str, Any]]:
    try:
        res = await get_rollout_history(context, kubeconfig_path, namespace, kind_lower, name)
        return res.get("revisions") or []
    except Exception:
        return []


async def _safe_helm_history(
    context: str, kubeconfig_path: Path, namespace: str, release_name: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not release_name:
        return None
    try:
        return await get_helm_release_history(context, kubeconfig_path, namespace, release_name, max_revisions=25)
    except Exception:
        return None


async def _safe_secrets_metadata(
    context: str, kubeconfig_path: Path, namespace: str
) -> List[Dict[str, Any]]:
    try:
        return await get_secrets_metadata(context, kubeconfig_path, namespace)
    except Exception:
        return []


async def get_workload_timeline(
    context: str,
    kubeconfig_path: Path,
    workload_type: str,
    name: str,
    namespace: str,
    limit: int = 50,
    ttl: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch live + historical rollout, Helm, ConfigMap/Secret metadata, and Event timeline."""
    canonical_kind, plural_res = normalize_workload_kind(workload_type)
    settings = get_config_store().settings
    cache_ttl = ttl if ttl is not None else settings.kube_cache_ttl_seconds

    workloads_res, rs_res, cm_res, sec_meta, ext_events, rollout_revs = await asyncio.gather(
        cached_kubectl_list(context, kubeconfig_path, plural_res, namespace, cache_ttl),
        cached_kubectl_list(context, kubeconfig_path, "replicasets", namespace, cache_ttl, optional=True),
        cached_kubectl_list(context, kubeconfig_path, "configmaps", namespace, cache_ttl, optional=True),
        _safe_secrets_metadata(context, kubeconfig_path, namespace),
        get_extended_events(context, kubeconfig_path, namespace=namespace, limit=200),
        _safe_rollout_history(context, kubeconfig_path, namespace, canonical_kind.lower(), name),
    )

    workload_obj = next(
        (w for w in workloads_res[0] if (w.get("metadata") or {}).get("name") == name),
        None,
    )
    if workload_obj is None:
        raise ValueError(f"{canonical_kind} '{namespace}/{name}' not found.")

    w_meta = workload_obj.get("metadata") or {}
    w_anns = w_meta.get("annotations") or {}
    w_labels = w_meta.get("labels") or {}
    release_name = w_anns.get("meta.helm.sh/release-name") or w_labels.get("app.kubernetes.io/instance")
    release_ns = w_anns.get("meta.helm.sh/release-namespace") or namespace
    helm_hist = await _safe_helm_history(context, kubeconfig_path, release_ns, release_name)

    return build_workload_timeline(
        workload_type=canonical_kind,
        name=name,
        namespace=namespace,
        workload_obj=workload_obj,
        replicasets=rs_res[0],
        rollout_revisions=rollout_revs,
        helm_history=helm_hist,
        configmaps=cm_res[0],
        secrets_meta=sec_meta,
        events=ext_events.get("items") or [],
        limit=limit,
    )
