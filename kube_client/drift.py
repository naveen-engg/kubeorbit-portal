"""Cross-cluster / Staging-vs-Prod Workload Drift Diff (read-only & Secret-masked).

Compares the live configuration and security posture of a Deployment, StatefulSet,
or DaemonSet between any two clusters in `clusters.yaml` (e.g., staging vs. prod):
- Replicas & ready count
- Container images per container name
- CPU & Memory requests / limits
- Container environment variables & `envFrom` references (sensitive values hashed
  server-side as `sha256:<8hex>`, `secretKeyRef` shown as `secret:<name>/<key>`)
- Liveness / Readiness / Startup probe definitions
- Referenced ConfigMap keys (key existence & deterministic SHA-256 fingerprints)
- Selecting NetworkPolicies & policyTypes
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config_store

from ._fetch import cached_kubectl_list
from .timeline import extract_referenced_config_objects, normalize_workload_kind

_SENSITIVE_KEY_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL|AUTH|DSN|CONNECTION_STRING)",
    re.IGNORECASE,
)


def _sha256_mask(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
    return f"sha256:{digest}"


def _mask_sensitive_env(key: str, env_entry: Dict[str, Any]) -> str:
    val_from = env_entry.get("valueFrom") or {}
    if "secretKeyRef" in val_from:
        ref = val_from["secretKeyRef"] or {}
        return f"secret:{ref.get('name', '?')}/{ref.get('key', '?')}"
    if "configMapKeyRef" in val_from:
        ref = val_from["configMapKeyRef"] or {}
        return f"configmap:{ref.get('name', '?')}/{ref.get('key', '?')}"
    if "fieldRef" in val_from:
        return f"fieldRef:{(val_from['fieldRef'] or {}).get('fieldPath', '?')}"
    if "resourceFieldRef" in val_from:
        return f"resourceFieldRef:{(val_from['resourceFieldRef'] or {}).get('resource', '?')}"

    raw_val = env_entry.get("value")
    if raw_val is None:
        return "<unset>"
    if _SENSITIVE_KEY_RE.search(key):
        return f"{_sha256_mask(raw_val)} (masked)"
    return str(raw_val)


def _selector_matches(labels: Dict[str, str], selector: Optional[Dict[str, Any]]) -> bool:
    if selector is None:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != str(v):
            return False
    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key")
        op = expr.get("operator")
        vals = [str(v) for v in (expr.get("values") or [])]
        if op == "In" and (key not in labels or labels.get(key) not in vals):
            return False
        if op == "NotIn" and (key in labels and labels.get(key) in vals):
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _probe_summary(probe: Optional[Dict[str, Any]]) -> str:
    if not probe:
        return "none"
    handler = "custom"
    if "httpGet" in probe:
        hg = probe["httpGet"] or {}
        handler = f"http:{hg.get('path', '/')}@{hg.get('port', '')}"
    elif "tcpSocket" in probe:
        handler = f"tcp:{(probe['tcpSocket'] or {}).get('port', '')}"
    elif "exec" in probe:
        cmd = (probe["exec"] or {}).get("command") or []
        handler = f"exec:{' '.join(str(c) for c in cmd[:3])}"
    elif "grpc" in probe:
        handler = f"grpc:{(probe['grpc'] or {}).get('port', '')}"
    return (
        f"{handler} "
        f"(delay={probe.get('initialDelaySeconds', 0)}s,"
        f"period={probe.get('periodSeconds', 10)}s,"
        f"fail={probe.get('failureThreshold', 3)})"
    )


def _extract_container_map(obj: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    pod_spec = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
    return {c.get("name") or f"container-{idx}": c for idx, c in enumerate(pod_spec.get("containers") or [])}


def _env_from_refs(container: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for ef in container.get("envFrom") or []:
        if "configMapRef" in ef:
            refs.append(f"cm:{(ef['configMapRef'] or {}).get('name', '?')}")
        elif "secretRef" in ef:
            refs.append(f"secret:{(ef['secretRef'] or {}).get('name', '?')}")
    return sorted(refs)


def _matching_netpol_signatures(workload_obj: Dict[str, Any], netpols: List[Dict[str, Any]]) -> List[str]:
    pod_labels = (((workload_obj.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
    matched: List[str] = []
    for pol in netpols:
        spec = pol.get("spec") or {}
        pod_sel = spec.get("podSelector")
        if pod_sel is None:
            pod_sel = {}
        if _selector_matches(pod_labels, pod_sel):
            name = (pol.get("metadata") or {}).get("name") or "unknown"
            ptypes = "/".join(spec.get("policyTypes") or ["Ingress"])
            matched.append(f"{name}[{ptypes}]")
    return sorted(matched)


def _append_diff(
    sections: List[Dict[str, Any]],
    category: str,
    field: str,
    source_val: Any,
    target_val: Any,
    *,
    severity_hint: str = "changed",
) -> None:
    s_str = str(source_val) if source_val is not None else "<unset>"
    t_str = str(target_val) if target_val is not None else "<unset>"
    if s_str != t_str:
        sections.append({
            "category": category,
            "field": field,
            "source_value": s_str,
            "target_value": t_str,
            "status": severity_hint,
        })


def compute_workload_drift(
    *,
    source_label: str,
    target_label: str,
    workload_type: str,
    name: str,
    source_obj: Dict[str, Any],
    target_obj: Optional[Dict[str, Any]],
    source_configmaps: Optional[List[Dict[str, Any]]] = None,
    target_configmaps: Optional[List[Dict[str, Any]]] = None,
    source_netpols: Optional[List[Dict[str, Any]]] = None,
    target_netpols: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compare two workload snapshots cleanly and return structured drift items."""
    canonical_kind, _ = normalize_workload_kind(workload_type)
    sections: List[Dict[str, Any]] = []

    if target_obj is None:
        sections.append({
            "category": "workload",
            "field": "existence",
            "source_value": f"{canonical_kind}/{name} (present)",
            "target_value": "missing on target cluster",
            "status": "only_in_source",
        })
        return {
            "workload_type": canonical_kind,
            "name": name,
            "source": {"label": source_label},
            "target": {"label": target_label},
            "has_drift": True,
            "drift_count": 1,
            "severity": "critical",
            "masked_server_side": True,
            "sections": sections,
            "summary": f"{canonical_kind}/{name} exists in {source_label} but is missing in {target_label}.",
        }

    # 1. Replicas
    s_reps = (source_obj.get("spec") or {}).get("replicas")
    t_reps = (target_obj.get("spec") or {}).get("replicas")
    if s_reps is not None or t_reps is not None:
        _append_diff(sections, "replicas", "spec.replicas", s_reps, t_reps)

    # 2. Containers: image, resources, env, probes
    s_containers = _extract_container_map(source_obj)
    t_containers = _extract_container_map(target_obj)
    for cname in sorted(set(s_containers) | set(t_containers)):
        sc = s_containers.get(cname)
        tc = t_containers.get(cname)
        if sc is None:
            _append_diff(sections, "image", f"containers[{cname}]", "<missing>", tc.get("image"), severity_hint="only_in_target")
            continue
        if tc is None:
            _append_diff(sections, "image", f"containers[{cname}]", sc.get("image"), "<missing>", severity_hint="only_in_source")
            continue

        _append_diff(sections, "image", f"containers[{cname}].image", sc.get("image"), tc.get("image"))

        s_res = sc.get("resources") or {}
        t_res = tc.get("resources") or {}
        for scope in ("requests", "limits"):
            for metric in ("cpu", "memory"):
                sv = (s_res.get(scope) or {}).get(metric)
                tv = (t_res.get(scope) or {}).get(metric)
                _append_diff(sections, "resources", f"containers[{cname}].{scope}.{metric}", sv, tv)

        s_envs = {e.get("name"): _mask_sensitive_env(e.get("name", ""), e) for e in (sc.get("env") or []) if e.get("name")}
        t_envs = {e.get("name"): _mask_sensitive_env(e.get("name", ""), e) for e in (tc.get("env") or []) if e.get("name")}
        for ek in sorted(set(s_envs) | set(t_envs)):
            sv = s_envs.get(ek, "<missing>")
            tv = t_envs.get(ek, "<missing>")
            status = "changed"
            if sv == "<missing>":
                status = "only_in_target"
            elif tv == "<missing>":
                status = "only_in_source"
            _append_diff(sections, "env", f"containers[{cname}].env.{ek}", sv, tv, severity_hint=status)

        _append_diff(sections, "env", f"containers[{cname}].envFrom", ", ".join(_env_from_refs(sc)) or "none", ", ".join(_env_from_refs(tc)) or "none")

        for probe_key in ("livenessProbe", "readinessProbe", "startupProbe"):
            _append_diff(
                sections,
                "probes",
                f"containers[{cname}].{probe_key}",
                _probe_summary(sc.get(probe_key)),
                _probe_summary(tc.get(probe_key)),
            )

    # 3. Referenced ConfigMaps (presence + SHA-256 key fingerprints)
    s_cm_refs, _ = extract_referenced_config_objects(source_obj)
    t_cm_refs, _ = extract_referenced_config_objects(target_obj)
    s_cm_map = {(cm.get("metadata") or {}).get("name"): cm for cm in (source_configmaps or [])}
    t_cm_map = {(cm.get("metadata") or {}).get("name"): cm for cm in (target_configmaps or [])}
    for cm_name in sorted(set(s_cm_refs) | set(t_cm_refs)):
        s_cm = s_cm_map.get(cm_name)
        t_cm = t_cm_map.get(cm_name)
        s_data = (s_cm.get("data") or {}) if s_cm else {}
        t_data = (t_cm.get("data") or {}) if t_cm else {}
        if s_cm is None and t_cm is not None:
            _append_diff(sections, "configmap", f"ConfigMap/{cm_name}", "<missing>", f"present ({len(t_data)} keys)", severity_hint="only_in_target")
            continue
        if t_cm is None and s_cm is not None:
            _append_diff(sections, "configmap", f"ConfigMap/{cm_name}", f"present ({len(s_data)} keys)", "<missing>", severity_hint="only_in_source")
            continue
        for k in sorted(set(s_data) | set(t_data)):
            sv = _sha256_mask(s_data[k]) if k in s_data else "<missing>"
            tv = _sha256_mask(t_data[k]) if k in t_data else "<missing>"
            _append_diff(sections, "configmap", f"ConfigMap/{cm_name}.{k}", sv, tv)

    # 4. NetworkPolicies matching workload PodTemplate labels
    s_npol = _matching_netpol_signatures(source_obj, source_netpols or [])
    t_npol = _matching_netpol_signatures(target_obj, target_netpols or [])
    if s_npol != t_npol:
        _append_diff(
            sections,
            "networkpolicy",
            "matching_network_policies",
            ", ".join(s_npol) or "none (default-allow)",
            ", ".join(t_npol) or "none (default-allow)",
        )

    drift_count = len(sections)
    has_drift = drift_count > 0
    categories = {s["category"] for s in sections}
    if any(s["status"] in ("only_in_source", "only_in_target") for s in sections) or "image" in categories or "networkpolicy" in categories:
        severity = "high" if has_drift else "none"
    elif has_drift:
        severity = "medium"
    else:
        severity = "none"

    summary = (
        f"No configuration drift detected between {source_label} and {target_label} for {canonical_kind}/{name}."
        if not has_drift
        else f"Detected {drift_count} drift difference(s) across {', '.join(sorted(categories))} between {source_label} and {target_label}."
    )

    return {
        "workload_type": canonical_kind,
        "name": name,
        "source": {"label": source_label},
        "target": {"label": target_label},
        "has_drift": has_drift,
        "drift_count": drift_count,
        "severity": severity,
        "masked_server_side": True,
        "sections": sections,
        "summary": summary,
    }


async def diff_workload_across_clusters(
    *,
    source_context: str,
    source_kubeconfig: Path,
    target_context: str,
    target_kubeconfig: Path,
    source_label: str,
    target_label: str,
    workload_type: str,
    name: str,
    source_namespace: str,
    target_namespace: Optional[str] = None,
    ttl: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch source & target workload, ConfigMaps, and NetworkPolicies concurrently and diff cleanly."""
    canonical_kind, plural_res = normalize_workload_kind(workload_type)
    settings = get_config_store().settings
    cache_ttl = ttl if ttl is not None else settings.kube_cache_ttl_seconds
    dst_ns = target_namespace or source_namespace

    s_w_res, s_cm_res, s_np_res, t_w_res, t_cm_res, t_np_res = await asyncio.gather(
        cached_kubectl_list(source_context, source_kubeconfig, plural_res, source_namespace, cache_ttl),
        cached_kubectl_list(source_context, source_kubeconfig, "configmaps", source_namespace, cache_ttl, optional=True),
        cached_kubectl_list(source_context, source_kubeconfig, "networkpolicies", source_namespace, cache_ttl, optional=True),
        cached_kubectl_list(target_context, target_kubeconfig, plural_res, dst_ns, cache_ttl),
        cached_kubectl_list(target_context, target_kubeconfig, "configmaps", dst_ns, cache_ttl, optional=True),
        cached_kubectl_list(target_context, target_kubeconfig, "networkpolicies", dst_ns, cache_ttl, optional=True),
    )

    src_obj = next((w for w in s_w_res[0] if (w.get("metadata") or {}).get("name") == name), None)
    if src_obj is None:
        raise ValueError(f"{canonical_kind} '{source_namespace}/{name}' not found on source cluster.")

    dst_obj = next((w for w in t_w_res[0] if (w.get("metadata") or {}).get("name") == name), None)

    result = compute_workload_drift(
        source_label=f"{source_label} ({source_namespace})",
        target_label=f"{target_label} ({dst_ns})",
        workload_type=canonical_kind,
        name=name,
        source_obj=src_obj,
        target_obj=dst_obj,
        source_configmaps=s_cm_res[0],
        target_configmaps=t_cm_res[0],
        source_netpols=s_np_res[0],
        target_netpols=t_np_res[0],
    )
    result["source_namespace"] = source_namespace
    result["target_namespace"] = dst_ns
    return result
