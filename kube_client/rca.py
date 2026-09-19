"""Issue detection and rule-based root-cause analysis (RCA).

Two kinds of findings are folded into the same `root_causes` list:
  1. Active failures -- pods that are actually broken right now
     (CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending).
  2. Proactive best-practice checks -- static, deterministic checks that
     flag risk on an otherwise "healthy" cluster (missing resource
     requests/limits, no liveness+readiness probes, `:latest` image tags,
     single-replica workloads, namespaces with zero NetworkPolicies). These
     run unconditionally alongside the failure scan so the view is useful
     even when nothing is currently on fire.

NOTE: `root_causes[].severity`/`confidence`/`blast_radius`/`remediation`
shape below is what static/app.js's renderDiagnosticsRCA() actually reads.
A prior version of this function returned a differently-shaped
`folded_issues` list that the frontend never read -- this view always
rendered "Zero Anomalies Detected" regardless of real cluster state. Both
shapes are returned now (folded_issues/raw_issues/health_score kept for any
other existing caller, e.g. the MCP tool) so nothing else breaks.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import get_config_store

from ._fetch import cached_kubectl_list
from .logs import get_pod_logs
from .resources import get_daemonsets, get_deployments, get_events, get_nodes, get_pods, get_statefulsets

# Trailing ReplicaSet-hash + pod-hash suffix on a typical Deployment-owned pod
# name (e.g. "web-frontend-65b4b9b6d4-r65nd" -> "web-frontend"), used only as
# a readable grouping label for best-practice findings -- not identity.
_POD_HASH_SUFFIX_RE = re.compile(r"(-[a-f0-9]{6,10})?-[a-z0-9]{5}$")


def _owner_label(pod: Dict[str, Any]) -> str:
    for ref in pod.get("metadata", {}).get("ownerReferences", []) or []:
        if ref.get("controller"):
            return f"{ref.get('kind', 'Pod')}/{ref.get('name')}"
    name = pod.get("metadata", {}).get("name", "")
    stripped = _POD_HASH_SUFFIX_RE.sub("", name)
    return f"Pod/{stripped or name}"


async def diagnose_cluster_issues(
    context: str, kubeconfig: Path, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Scan cluster for degraded pods and best-practice gaps, fold both into
    root causes, and produce remediation suggestions."""
    ns = namespace if namespace else ""
    ttl = get_config_store().settings.kube_cache_ttl_seconds

    pods, events, nodes, deployments, statefulsets, daemonsets, raw_pods = await asyncio.gather(
        get_pods(context, kubeconfig, ns),
        get_events(context, kubeconfig, ns, limit=100),
        get_nodes(context, kubeconfig),
        get_deployments(context, kubeconfig, ns),
        get_statefulsets(context, kubeconfig, ns),
        get_daemonsets(context, kubeconfig, ns),
        cached_kubectl_list(context, kubeconfig, "pods", ns, ttl),
    )
    raw_pod_items = raw_pods[0]

    if namespace is None:
        # Cluster-wide scan: exclude system namespaces, same as
        # get_workloads_overview/get_events -- kube-system's vendor-managed
        # pods missing resource limits/probes isn't actionable signal for
        # whoever's looking at this cluster's own workloads, and including
        # it drowns out real findings and skews the score unfairly.
        store = get_config_store()
        pods = [p for p in pods if not store.is_namespace_excluded(p["namespace"])]
        deployments = [d for d in deployments if not store.is_namespace_excluded(d["namespace"])]
        statefulsets = [s for s in statefulsets if not store.is_namespace_excluded(s["namespace"])]
        daemonsets = [ds for ds in daemonsets if not store.is_namespace_excluded(ds["namespace"])]
        raw_pod_items = [
            rp for rp in raw_pod_items
            if not store.is_namespace_excluded(rp.get("metadata", {}).get("namespace", ""))
        ]

    raw_pod_by_name = {rp.get("metadata", {}).get("name"): rp for rp in raw_pod_items}

    findings: List[Dict[str, Any]] = []

    # -- 1. Active pod-level failures ---------------------------------------
    for p in pods:
        p_name = p["name"]
        p_ns = p["namespace"]
        status = p["status"]
        restarts = p.get("restarts", 0)

        is_issue = False
        issue_type = None
        severity = "critical"

        if "CrashLoopBackOff" in status or "Error" in status:
            is_issue, issue_type = True, "Container CrashLoop"
        elif "OOMKilled" in status or restarts >= 3:
            is_issue, issue_type = True, "Frequent Restarts / Memory Pressure"
        elif "ImagePullBackOff" in status or "ErrImagePull" in status:
            is_issue, issue_type = True, "Image Pull Failure"
        elif status == "Pending":
            is_issue, issue_type, severity = True, "Pending / Unschedulable", "warning"

        if not is_issue:
            continue

        pod_events = [
            e for e in events
            if e.get("involved_name") == p_name or p_name in (e.get("message") or "")
        ]
        latest_msg = pod_events[0]["message"] if pod_events else "Container exiting with non-zero status"

        pod_logs_snippet = ""
        try:
            pod_logs_snippet = await get_pod_logs(context, kubeconfig, p_ns, p_name, tail_lines=20)
        except Exception:
            pod_logs_snippet = latest_msg

        root_cause = "Application crashed on startup"
        remediation_cmd = f"kubectl logs {p_name} -n {p_ns} --previous"
        confidence = 0.90

        if "OOM" in pod_logs_snippet or "memory" in pod_logs_snippet.lower():
            root_cause = "Out of Memory (OOMKilled) — container memory limit exceeded."
            remediation_cmd = (
                f"kubectl set resources deployment "
                f"$(kubectl get pod {p_name} -n {p_ns} -o jsonpath='{{.metadata.ownerReferences[0].name}}') "
                f"-n {p_ns} --limits=memory=256Mi"
            )
            confidence = 0.98
        elif "Image Pull" in issue_type:
            root_cause = "Container image repository returned 404/unauthorized or the image tag does not exist."
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 5 Events"
            confidence = 0.99
        elif "CrashLoop" in issue_type:
            root_cause = f"Process terminated prematurely (non-zero exit). Log snippet: '{pod_logs_snippet[:120]}...'"
            remediation_cmd = f"kubectl logs {p_name} -n {p_ns} --previous"
            confidence = 0.90
        elif "Pending" in issue_type:
            root_cause = "Pod cannot be scheduled — likely insufficient node resources or an unsatisfied node/taint selector."
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 10 Events"
            confidence = 0.80

        raw_pod = raw_pod_by_name.get(p_name)
        owner_label = _owner_label(raw_pod) if raw_pod else f"Pod/{p_name}"

        findings.append({
            "kind": "active_failure",
            "namespace": p_ns,
            "issue_type": issue_type,
            "severity": severity,
            "confidence": confidence,
            "root_cause": root_cause,
            "remediation_cmd": remediation_cmd,
            "resource_name": p_name,
            "impacted_service": owner_label.split("/", 1)[-1],
            "sample": {
                "resource_type": "Pod", "resource_name": p_name, "namespace": p_ns,
                "status": status, "restarts": restarts, "latest_event": latest_msg,
                "log_snippet": pod_logs_snippet[-300:] if pod_logs_snippet else "",
            },
        })

    # -- 2. Proactive best-practice checks (run regardless of failures) -----
    workload_by_name = {d["name"]: d for d in [*deployments, *statefulsets]}

    for pod in raw_pod_items:
        meta = pod.get("metadata", {})
        p_ns, p_name = meta.get("namespace"), meta.get("name")
        owner = _owner_label(pod)
        containers = pod.get("spec", {}).get("containers", []) or []

        for c in containers:
            resources = c.get("resources", {}) or {}
            if not resources.get("requests") and not resources.get("limits"):
                findings.append({
                    "kind": "best_practice", "namespace": p_ns,
                    "issue_type": "Missing Resource Requests/Limits",
                    "severity": "warning", "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} has no CPU/memory requests or limits set, "
                                  "so the scheduler can't reason about its footprint and it can starve neighbors under load.",
                    "remediation_cmd": f"kubectl set resources {owner.lower()} -n {p_ns} --requests=cpu=100m,memory=128Mi --limits=cpu=500m,memory=512Mi",
                    "resource_name": owner, "impacted_service": owner,
                })

            if not c.get("livenessProbe") and not c.get("readinessProbe"):
                findings.append({
                    "kind": "best_practice", "namespace": p_ns,
                    "issue_type": "No Liveness/Readiness Probes",
                    "severity": "warning", "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} has neither a liveness nor a readiness probe, "
                                  "so Kubernetes can't detect a hung/unready process and traffic may reach it too early.",
                    "remediation_cmd": f"kubectl edit {owner.lower()} -n {p_ns}  # add livenessProbe/readinessProbe",
                    "resource_name": owner, "impacted_service": owner,
                })

            image = c.get("image", "") or ""
            if image.endswith(":latest") or (":" not in image.split("/")[-1] and "@" not in image):
                findings.append({
                    "kind": "best_practice", "namespace": p_ns,
                    "issue_type": "Untagged / :latest Image",
                    "severity": "info", "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} runs '{image}', which is not reproducible — "
                                  "a redeploy can silently pull a different image than what's running now.",
                    "remediation_cmd": f"kubectl set image {owner.lower()} -n {p_ns} {c.get('name')}={image.split(':')[0]}:<pinned-version>",
                    "resource_name": owner, "impacted_service": owner,
                })

    for w in [*deployments, *statefulsets]:
        if w.get("desired") == 1:
            owner = w["name"]
            findings.append({
                "kind": "best_practice", "namespace": w["namespace"],
                "issue_type": "Single-Replica Workload",
                "severity": "info", "confidence": 1.0,
                "root_cause": f"'{owner}' runs a single replica — a node drain, OOM, or crash causes full downtime "
                              "for it with no rolling-update headroom.",
                "remediation_cmd": f"kubectl scale deployment/{owner} -n {w['namespace']} --replicas=2",
                "resource_name": owner, "impacted_service": owner,
            })

    namespaces_with_workloads = {w["namespace"] for w in [*deployments, *statefulsets, *daemonsets]}
    for check_ns in namespaces_with_workloads:
        netpols, available = await cached_kubectl_list(context, kubeconfig, "networkpolicies", check_ns, ttl, optional=True)
        if available and not netpols:
            findings.append({
                "kind": "best_practice", "namespace": check_ns,
                "issue_type": "No NetworkPolicy in Namespace",
                "severity": "info", "confidence": 1.0,
                "root_cause": f"Namespace '{check_ns}' has workloads but zero NetworkPolicies — every pod can reach "
                              "every other pod cluster-wide by default (no network segmentation).",
                "remediation_cmd": f"kubectl get networkpolicy -n {check_ns}  # then apply a default-deny baseline",
                "resource_name": check_ns, "impacted_service": check_ns,
            })

    # -- 3. Fold findings into root causes (group by namespace + issue_type) -
    folded_groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        folded_groups.setdefault(f"{f['namespace']}/{f['issue_type']}", []).append(f)

    root_causes = []
    raw_issues = []  # backward-compat flat list (old shape some callers may still read)
    for group in folded_groups.values():
        primary = group[0]
        affected = sorted({g["resource_name"] for g in group})
        root_causes.append({
            "id": f"rc-{primary['namespace']}-{primary['issue_type']}".lower().replace(" ", "-").replace("/", "-"),
            "severity": primary["severity"],
            "confidence": primary["confidence"],
            "namespace": primary["namespace"],
            "title": f"{primary['issue_type']} — {len(group)} resource(s) in '{primary['namespace']}'"
                      if len(group) > 1 else f"{primary['issue_type']} — {affected[0]}",
            "summary": primary["root_cause"],
            "blast_radius": {
                "impacted_services": sorted({g["impacted_service"] for g in group}),
                "impacted_namespaces": [primary["namespace"]],
                "blast_scope": "cluster-wide" if len(namespaces_with_workloads) > 1 and primary["issue_type"] == "No NetworkPolicy in Namespace" else "isolated",
            },
            "folded_failures": affected,
            "remediation": {"command": primary["remediation_cmd"], "action": primary["remediation_cmd"]},
        })
        for g in group:
            raw_issues.append({
                "resource_type": g.get("sample", {}).get("resource_type", "Workload"),
                "resource_name": g["resource_name"],
                "namespace": g["namespace"],
                "issue_type": g["issue_type"],
                "severity": g["severity"].upper(),
                "root_cause": g["root_cause"],
                "confidence": f"{int(g['confidence'] * 100)}%",
                "remediation_cmd": g["remediation_cmd"],
                **({"log_snippet": g["sample"]["log_snippet"]} if "sample" in g else {}),
            })

    active_failures = [f for f in findings if f["kind"] == "active_failure"]
    all_workloads = [*deployments, *statefulsets, *daemonsets]
    workloads_healthy = sum(1 for w in all_workloads if w.get("healthy", True))

    overall_score = max(0, 100 - len(active_failures) * 15 - sum(1 for f in findings if f["kind"] == "best_practice") * 4)

    return {
        # Shape static/app.js's renderDiagnosticsRCA() actually renders:
        "summary": {
            "overall_score": overall_score,
            "active_root_causes": len(root_causes),
            "workloads_healthy": workloads_healthy,
            "workloads_total": len(all_workloads),
        },
        "root_causes": sorted(root_causes, key=lambda r: {"critical": 0, "warning": 1, "info": 2}.get(r["severity"], 3)),
        # Kept for backward compatibility with any other existing caller
        # (e.g. the MCP tool's docstring references health_score):
        "health_score": overall_score,
        "total_issues": len(findings),
        "folded_count": len(root_causes),
        "folded_issues": root_causes,
        "raw_issues": raw_issues,
    }
