"""Issue detection, rule-based root-cause analysis (RCA), and full 12-Step
simulation mapped to the 12-Step `kubectl apply` Machine:

  Step 1  : kubectl client (kubeconfig, Server-Side Apply / field managers)
  Step 2  : kube-apiserver {authn} (X.509 / OIDC / ServiceAccount token)
  Step 3  : kube-apiserver {rbac-authz} (Role / ClusterRole / RoleBinding)
  Step 4  : kube-apiserver {mutating-admission} (defaults, LimitRange, sidecars)
  Step 5  : kube-apiserver {schema-validation} & {validating-admission}
  Step 6  : etcd · cluster state (object persistence & state size)
  Step 7  : kube-controller-manager {deployment-controller} (rollout -> ReplicaSet)
  Step 8  : kube-controller-manager {replicaset-controller} & {pod-security}
  Step 9  : kube-scheduler {filter} -> {score} -> {bind}
  Step 10 : kubelet · worker node (10a CSI, 10b CNI, 10c Image Pull, 10d containerd)
  Step 11 : kubelet {startup-probe} & {readiness-probe}
  Step 12 : {endpointslice-controller} -> EndpointSlice -> kube-proxy / eBPF CNI
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config_store

from ._fetch import cached_kubectl_list
from .logs import get_pod_logs
from .resources import (
    get_configmaps,
    get_daemonsets,
    get_deployments,
    get_events,
    get_nodes,
    get_pods,
    get_services,
    get_statefulsets,
)

_POD_HASH_SUFFIX_RE = re.compile(r"(-[a-f0-9]{6,10})?-[a-z0-9]{5}$")
_MAX_LOG_FETCH_PODS = 10

# Canonical Kubernetes components across the 12-Step Machine (used for UI filtering)
K8S_COMPONENT_CATALOG: List[Dict[str, Any]] = [
    {
        "component_id": "kubectl-client",
        "label": "kubectl · Client",
        "steps": [1],
        "step_range": "Step 1",
    },
    {
        "component_id": "kube-apiserver",
        "label": "kube-apiserver",
        "steps": [2, 3, 4, 5],
        "step_range": "Steps 2–5",
    },
    {
        "component_id": "etcd",
        "label": "etcd · State",
        "steps": [6],
        "step_range": "Step 6",
    },
    {
        "component_id": "kube-controller-manager",
        "label": "kube-controller-manager",
        "steps": [7, 8],
        "step_range": "Steps 7–8",
    },
    {
        "component_id": "kube-scheduler",
        "label": "kube-scheduler",
        "steps": [9],
        "step_range": "Step 9",
    },
    {
        "component_id": "kubelet-runtime",
        "label": "kubelet · CSI / CNI / Runtime",
        "steps": [10],
        "step_range": "Step 10",
    },
    {
        "component_id": "kubelet-probes",
        "label": "kubelet · Probes",
        "steps": [11],
        "step_range": "Step 11",
    },
    {
        "component_id": "datapath-routing",
        "label": "EndpointSlice & kube-proxy",
        "steps": [12],
        "step_range": "Step 12",
    },
]

# Canonical 12-Step pipeline stage metadata returned with every RCA response
PIPELINE_STAGE_CATALOG: List[Dict[str, Any]] = [
    {
        "stage_id": "steps-1-6",
        "steps": "Steps 1–6",
        "title": "API Server & etcd",
        "component": "kube-apiserver → etcd",
        "detail": "1 kubectl → 2 authn → 3 rbac → 4 mutating → 5 validating → 6 etcd",
    },
    {
        "stage_id": "steps-7-8",
        "steps": "Steps 7–8",
        "title": "Controllers & PodSecurity",
        "component": "kube-controller-manager",
        "detail": "7 Deployment → 8 ReplicaSet → Pod creation (PodSecurity & Quota)",
    },
    {
        "stage_id": "step-9",
        "steps": "Step 9",
        "title": "Scheduler",
        "component": "kube-scheduler",
        "detail": "Filter (CPU/Mem/Taints/Affinity/PVC) → Score → Bind node",
    },
    {
        "stage_id": "step-10",
        "steps": "Step 10",
        "title": "Kubelet (CSI / CNI / Runtime)",
        "component": "kubelet · worker node",
        "detail": "10a CSI Mount → 10b CNI IP → 10c Image Pull → 10d Containerd",
    },
    {
        "stage_id": "step-11",
        "steps": "Step 11",
        "title": "Readiness & Startup Probes",
        "component": "kubelet · probes",
        "detail": "HTTP / TCP / Exec readinessProbe passes → Pod Ready=True",
    },
    {
        "stage_id": "step-12",
        "steps": "Step 12",
        "title": "EndpointSlice & Traffic",
        "component": "endpointslice-controller / kube-proxy",
        "detail": "Service selector matches Ready Pod IP → EndpointSlice → Traffic flows",
    },
]


def _owner_label(pod: Dict[str, Any]) -> str:
    for ref in pod.get("metadata", {}).get("ownerReferences", []) or []:
        if ref.get("controller"):
            return f"{ref.get('kind', 'Pod')}/{ref.get('name')}"
    name = pod.get("metadata", {}).get("name", "")
    stripped = _POD_HASH_SUFFIX_RE.sub("", name)
    return f"Pod/{stripped or name}"


def _pod_conditions_map(raw_pod: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not raw_pod:
        return {}
    conds = raw_pod.get("status", {}).get("conditions", []) or []
    return {c.get("type"): c for c in conds if c.get("type")}


def _waiting_reasons(raw_pod: Optional[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    if not raw_pod:
        return []
    out = []
    status = raw_pod.get("status", {}) or {}
    for cs in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason"):
            out.append((cs.get("name", ""), w.get("reason", ""), w.get("message", "")))
    return out


def _infer_component_id(step_num: int) -> str:
    if step_num == 1:
        return "kubectl-client"
    if step_num in (2, 3, 4, 5):
        return "kube-apiserver"
    if step_num == 6:
        return "etcd"
    if step_num in (7, 8):
        return "kube-controller-manager"
    if step_num == 9:
        return "kube-scheduler"
    if step_num == 10:
        return "kubelet-runtime"
    if step_num == 11:
        return "kubelet-probes"
    return "datapath-routing"


def _build_rca_payload(
    findings: List[Dict[str, Any]],
    workloads_healthy: int,
    workloads_total: int,
    namespaces_with_workloads: List[str],
    simulated: bool = False,
) -> Dict[str, Any]:
    folded_groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        folded_groups.setdefault(f"{f['namespace']}/{f['issue_type']}", []).append(f)

    root_causes = []
    raw_issues = []
    for group in folded_groups.values():
        primary = group[0]
        affected = sorted({g["resource_name"] for g in group})
        step_num = int(primary.get("step_number", 10))
        comp_id = primary.get("component_id") or _infer_component_id(step_num)
        root_causes.append({
            "id": f"rc-{primary['namespace']}-{primary['issue_type']}".lower().replace(" ", "-").replace("/", "-"),
            "kind": primary["kind"],
            "step_number": step_num,
            "stage_id": primary.get("stage_id", "step-10"),
            "step_badge": primary.get("step_badge", f"Step {step_num}"),
            "component": primary.get("component", "kubelet"),
            "component_id": comp_id,
            "severity": primary["severity"],
            "confidence": primary["confidence"],
            "namespace": primary["namespace"],
            "simulated": bool(primary.get("simulated", simulated)),
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
                "step_number": int(g.get("step_number", step_num)),
                "stage_id": g.get("stage_id", "step-10"),
                "step_badge": g.get("step_badge", f"Step {step_num}"),
                "component_id": g.get("component_id") or comp_id,
                "issue_type": g["issue_type"],
                "severity": g["severity"].upper(),
                "root_cause": g["root_cause"],
                "confidence": f"{int(g['confidence'] * 100)}%",
                "remediation_cmd": g["remediation_cmd"],
                **({"log_snippet": g["sample"]["log_snippet"]} if "sample" in g else {}),
            })

    active_failures = [f for f in findings if f["kind"] == "active_failure"]
    overall_score = max(0, 100 - len(active_failures) * 8 - sum(1 for f in findings if f["kind"] == "best_practice") * 2)

    pipeline_stages = []
    for stage in PIPELINE_STAGE_CATALOG:
        sid = stage["stage_id"]
        stage_active = [rc for rc in root_causes if rc.get("stage_id") == sid and rc.get("kind") == "active_failure"]
        stage_bp = [rc for rc in root_causes if rc.get("stage_id") == sid and rc.get("kind") == "best_practice"]
        status = "healthy"
        if any(rc["severity"] == "critical" for rc in stage_active):
            status = "critical"
        elif stage_active:
            status = "warning"
        pipeline_stages.append({
            **stage,
            "status": status,
            "active_failures": len(stage_active),
            "best_practice_warnings": len(stage_bp),
        })

    # Per-step (1..12) and per-component summaries for the infographic & filter bar
    step_counts: Dict[int, int] = {i: 0 for i in range(1, 13)}
    for rc in root_causes:
        if rc.get("kind") == "active_failure":
            s_num = int(rc.get("step_number", 10))
            step_counts[s_num] = step_counts.get(s_num, 0) + 1

    component_summary = []
    for comp in K8S_COMPONENT_CATALOG:
        cid = comp["component_id"]
        c_active = [rc for rc in root_causes if rc.get("component_id") == cid and rc.get("kind") == "active_failure"]
        c_bp = [rc for rc in root_causes if rc.get("component_id") == cid and rc.get("kind") == "best_practice"]
        component_summary.append({
            **comp,
            "active_failures": len(c_active),
            "advisories": len(c_bp),
            "total": len(c_active) + len(c_bp),
        })

    return {
        "simulated": simulated,
        "summary": {
            "overall_score": overall_score,
            "active_root_causes": len(root_causes),
            "active_failures_count": len(active_failures),
            "workloads_healthy": workloads_healthy,
            "workloads_total": workloads_total,
        },
        "step_counts": step_counts,
        "components": component_summary,
        "pipeline_stages": pipeline_stages,
        "root_causes": sorted(
            root_causes,
            key=lambda r: (
                0 if r.get("kind") == "active_failure" else 1,
                int(r.get("step_number", 10)),
                {"critical": 0, "warning": 1, "info": 2}.get(r["severity"], 3),
            ),
        ),
        "health_score": overall_score,
        "total_issues": len(findings),
        "folded_count": len(root_causes),
        "folded_issues": root_causes,
        "raw_issues": raw_issues,
    }


def get_simulated_12_step_findings(namespace: str = "kubeorbit-demo") -> List[Dict[str, Any]]:
    """Return realistic simulated failure findings covering ALL 12 steps of the
    `kubectl apply` Machine across every Kubernetes control-plane and data-plane component."""
    ns = namespace or "kubeorbit-demo"
    return [
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 1,
            "stage_id": "steps-1-6",
            "step_badge": "Step 1 · kubectl Server-Side Apply",
            "component": "kubectl · server-side-apply",
            "component_id": "kubectl-client",
            "issue_type": "Step 1: Server-Side Apply FieldManager Conflict",
            "severity": "warning",
            "confidence": 0.96,
            "root_cause": (
                "[Step 1 · kubectl / Server-Side Apply] Conflict on field '.spec.template.spec.containers[0].resources.limits.memory' "
                "between fieldManager 'kubectl-client-side-apply' and 'argocd-controller'. Manifest apply requires --force-conflicts or unified SSA ownership."
            ),
            "remediation_cmd": f"kubectl apply --server-side --force-conflicts -f sample-app.yaml -n {ns}",
            "resource_name": "Deployment/api-backend",
            "impacted_service": "api-backend",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 2,
            "stage_id": "steps-1-6",
            "step_badge": "Step 2 · kube-apiserver {authn}",
            "component": "kube-apiserver · authn",
            "component_id": "kube-apiserver",
            "issue_type": "Step 2: Expired / Unmounted ServiceAccount Token (401 Unauthorized)",
            "severity": "critical",
            "confidence": 0.98,
            "root_cause": (
                "[Step 2 · kube-apiserver {authn}] Workload 'telemetry-collector' disables automountServiceAccountToken while attempting "
                "in-cluster API authentication, resulting in '401 Unauthorized: You must be logged in to the server'."
            ),
            "remediation_cmd": f"kubectl patch sa default -n {ns} -p '{{\"automountServiceAccountToken\":true}}' && kubectl create token default -n {ns}",
            "resource_name": "ServiceAccount/telemetry-collector-sa",
            "impacted_service": "telemetry-collector",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 3,
            "stage_id": "steps-1-6",
            "step_badge": "Step 3 · kube-apiserver {rbac-authz}",
            "component": "kube-apiserver · rbac-authz",
            "component_id": "kube-apiserver",
            "issue_type": "Step 3: RBAC Forbidden (Missing RoleBinding Permission)",
            "severity": "critical",
            "confidence": 0.99,
            "root_cause": (
                f"[Step 3 · kube-apiserver {{rbac-authz}}] User 'system:serviceaccount:{ns}:ci-deployer' is forbidden from "
                f"patching resource 'deployments' in API group 'apps' in namespace '{ns}' (missing RoleBinding)."
            ),
            "remediation_cmd": f"kubectl auth can-i patch deployments -n {ns} --as=system:serviceaccount:{ns}:ci-deployer && kubectl get rolebindings -n {ns}",
            "resource_name": "RoleBinding/ci-deployer-ReadOnly",
            "impacted_service": "ci-deployer",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 4,
            "stage_id": "steps-1-6",
            "step_badge": "Step 4 · {mutating-admission}",
            "component": "kube-apiserver · mutating-admission",
            "component_id": "kube-apiserver",
            "issue_type": "Step 4: Mutating Admission Webhook Timeout (Sidecar Injector)",
            "severity": "warning",
            "confidence": 0.95,
            "root_cause": (
                "[Step 4 · kube-apiserver {mutating-admission}] MutatingWebhookConfiguration 'istio-sidecar-injector' failed to respond "
                "within 10s; default resource LimitRange & envoy proxy sidecar injection were skipped."
            ),
            "remediation_cmd": "kubectl get mutatingwebhookconfigurations && kubectl describe limitrange -n " + ns,
            "resource_name": "MutatingWebhook/sidecar-injector.istio.io",
            "impacted_service": "web-frontend",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 5,
            "stage_id": "steps-1-6",
            "step_badge": "Step 5 · {validating-admission}",
            "component": "kube-apiserver · validating-admission",
            "component_id": "kube-apiserver",
            "issue_type": "Step 5: Validating Admission Webhook Denied Request",
            "severity": "critical",
            "confidence": 0.99,
            "root_cause": (
                "[Step 5 · kube-apiserver {schema-validation & validating-admission}] Admission webhook 'validation.kyverno.svc' denied "
                "manifest: 'require-non-root-user: spec.template.spec.securityContext.runAsNonRoot must be set to true'."
            ),
            "remediation_cmd": f"kubectl apply --dry-run=server -f sample-app.yaml -n {ns} && kubectl get validatingwebhookconfigurations",
            "resource_name": "ValidatingPolicy/require-non-root-user",
            "impacted_service": "payment-db",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 6,
            "stage_id": "steps-1-6",
            "step_badge": "Step 6 · etcd · cluster state",
            "component": "etcd · cluster state",
            "component_id": "etcd",
            "issue_type": "Step 6: etcd Object Size Near 1MiB Limit & Compaction Lag",
            "severity": "warning",
            "confidence": 0.94,
            "root_cause": (
                "[Step 6 · etcd · cluster state] ConfigMap 'large-feature-bundle' and 48 unpruned ReplicaSet revisions in etcd are approaching "
                "etcd's 1.0 MiB single-object raft payload ceiling (only kube-apiserver talks to etcd)."
            ),
            "remediation_cmd": f"kubectl get cm,secrets -n {ns} -o json | jq '.items[] | {{name: .metadata.name, bytes: (. | tostring | length)}}'",
            "resource_name": "etcd/configmap-state-quota",
            "impacted_service": "api-backend",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 7,
            "stage_id": "steps-7-8",
            "step_badge": "Step 7 · {deployment-controller}",
            "component": "kube-controller-manager",
            "component_id": "kube-controller-manager",
            "issue_type": "Step 7: Deployment Rollout Stalled (ProgressDeadlineExceeded)",
            "severity": "critical",
            "confidence": 0.97,
            "root_cause": (
                "[Step 7 · {deployment-controller}] Deployment 'checkout-canary' exceeded its progressDeadlineSeconds (600s) waiting for new "
                "ReplicaSet 'checkout-canary-6874ddb49' to reach minimum availability."
            ),
            "remediation_cmd": f"kubectl rollout status deployment/checkout-canary -n {ns} && kubectl rollout undo deployment/checkout-canary -n {ns}",
            "resource_name": "Deployment/checkout-canary",
            "impacted_service": "checkout-canary",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 8,
            "stage_id": "steps-7-8",
            "step_badge": "Step 8 · {replicaset-controller} & {pod-security}",
            "component": "kube-controller-manager",
            "component_id": "kube-controller-manager",
            "issue_type": "Step 8: Pod Creation Blocked by PodSecurity / ResourceQuota (0 Pods Created)",
            "severity": "critical",
            "confidence": 0.99,
            "root_cause": (
                "[Step 8 · {replicaset-controller} + {pod-security}] ReplicaSet 'privileged-agent-rs' desires 2 pods but created 0. "
                "Event FailedCreate: pods 'privileged-agent-*' is forbidden: violates PodSecurity 'restricted:latest' (allowPrivilegeEscalation != false)."
            ),
            "remediation_cmd": f"kubectl describe rs -n {ns} | grep -A 8 'FailedCreate\\|PodSecurity'",
            "resource_name": "ReplicaSet/privileged-agent-7f8b9c",
            "impacted_service": "privileged-agent",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 9,
            "stage_id": "step-9",
            "step_badge": "Step 9 · kube-scheduler (Filter / Score / Bind)",
            "component": "kube-scheduler",
            "component_id": "kube-scheduler",
            "issue_type": "Step 9: Scheduler Stalled (Node Selector / Insufficient Capacity)",
            "severity": "warning",
            "confidence": 0.98,
            "root_cause": (
                "[Step 9 · kube-scheduler] Pod 'gpu-ml-inference-9d9885bcd-vnbb4' is stuck in Pending (spec.nodeName is empty). "
                "FailedScheduling: 0/1 nodes are available: 1 node(s) didn't match Pod's nodeSelector 'accelerator=nvidia-h100-gpu'."
            ),
            "remediation_cmd": f"kubectl describe pod -l app=gpu-ml-inference -n {ns} | grep -A 12 Events",
            "resource_name": "Pod/gpu-ml-inference-9d9885bcd-vnbb4",
            "impacted_service": "gpu-ml-inference",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 10,
            "stage_id": "step-10",
            "step_badge": "Step 10a/c/d · kubelet (CSI / CNI / Image / containerd)",
            "component": "kubelet · worker node",
            "component_id": "kubelet-runtime",
            "issue_type": "Step 10: Kubelet Stalls (CSI FailedMount + ImagePullBackOff + OOMKilled)",
            "severity": "critical",
            "confidence": 0.99,
            "root_cause": (
                "[Step 10 · kubelet · worker node] 3 worker-node stage failures detected: "
                "(10a CSI) 'audit-shipper-csi' stuck in ContainerCreating: secret 'missing-mtls-client-cert' not found; "
                "(10c Image) 'checkout-canary' in ImagePullBackOff for tag 'nginx:nonexistent-canary-v99.9.9'; "
                "(10d Runtime) 'faulty-worker' crashing with OOMKill / exit code 1."
            ),
            "remediation_cmd": f"kubectl describe pod -l 'app in (audit-shipper-csi,checkout-canary,faulty-worker)' -n {ns}",
            "resource_name": "Pod/audit-shipper-csi, Pod/checkout-canary, Pod/faulty-worker",
            "impacted_service": "audit-shipper-csi",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 11,
            "stage_id": "step-11",
            "step_badge": "Step 11 · kubelet {readiness-probe}",
            "component": "kubelet · readinessProbe",
            "component_id": "kubelet-probes",
            "issue_type": "Step 11: Readiness Probe Failing (Running 0/1 Ready)",
            "severity": "warning",
            "confidence": 0.98,
            "root_cause": (
                "[Step 11 · kubelet {readiness-probe}] Pod 'inventory-unready-67b5b95678-fv4qz' is Running in containerd, but HTTP "
                "readinessProbe on port 9099 (/healthz) failed with 'connection refused' (app listens on 8080). Pod is marked Ready=False."
            ),
            "remediation_cmd": f"kubectl patch deploy inventory-unready -n {ns} --type='json' -p='[{{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/readinessProbe/httpGet/port\",\"value\":8080}}]'",
            "resource_name": "Pod/inventory-unready-67b5b95678-fv4qz",
            "impacted_service": "inventory-unready",
        },
        {
            "kind": "active_failure",
            "simulated": True,
            "namespace": ns,
            "step_number": 12,
            "stage_id": "step-12",
            "step_badge": "Step 12 · {endpointslice-controller} & kube-proxy",
            "component": "endpointslice-controller / kube-proxy",
            "component_id": "datapath-routing",
            "issue_type": "Step 12: Service Selector Matches 0 Ready Endpoints (HTTP 503 at Ingress)",
            "severity": "critical",
            "confidence": 0.99,
            "root_cause": (
                "[Step 12 · {endpointslice-controller}] Service 'orphaned-orders-svc' selector 'app=orders-service-typo-v99' matches 0 pods. "
                "EndpointSlice has 0 ready addresses, so kube-proxy / eBPF CNI and Ingress return immediate 503 Service Unavailable."
            ),
            "remediation_cmd": f"kubectl get endpointslices -l kubernetes.io/service-name=orphaned-orders-svc -n {ns} && kubectl describe svc orphaned-orders-svc -n {ns}",
            "resource_name": "Service/orphaned-orders-svc",
            "impacted_service": "orphaned-orders-svc",
        },
    ]


def get_simulated_12_step_rca(namespace: str = "kubeorbit-demo") -> Dict[str, Any]:
    """Build a complete 12-Step RCA response payload with all 12 steps active."""
    ns = namespace or "kubeorbit-demo"
    findings = get_simulated_12_step_findings(ns)
    return _build_rca_payload(
        findings=findings,
        workloads_healthy=5,
        workloads_total=11,
        namespaces_with_workloads=[ns],
        simulated=True,
    )


async def diagnose_cluster_issues(
    context: str,
    kubeconfig: Path,
    namespace: Optional[str] = None,
    simulate_all_steps: bool = False,
) -> Dict[str, Any]:
    """Scan cluster across the 12-Step `kubectl apply` pipeline for stalled
    workloads and best-practice gaps, fold into root causes, and produce
    stage-specific remediation commands."""
    if simulate_all_steps:
        return get_simulated_12_step_rca(namespace or "kubeorbit-demo")

    ns = namespace if namespace else ""
    ttl = get_config_store().settings.kube_cache_ttl_seconds

    (
        pods,
        events,
        nodes,
        deployments,
        statefulsets,
        daemonsets,
        services,
        configmaps,
        raw_pods,
        raw_replicasets,
    ) = await asyncio.gather(
        get_pods(context, kubeconfig, ns),
        get_events(context, kubeconfig, ns, limit=200),
        get_nodes(context, kubeconfig),
        get_deployments(context, kubeconfig, ns),
        get_statefulsets(context, kubeconfig, ns),
        get_daemonsets(context, kubeconfig, ns),
        get_services(context, kubeconfig, ns),
        get_configmaps(context, kubeconfig, ns),
        cached_kubectl_list(context, kubeconfig, "pods", ns, ttl),
        cached_kubectl_list(context, kubeconfig, "replicasets", ns, ttl, optional=True),
    )
    raw_pod_items = raw_pods[0]
    raw_rs_items = raw_replicasets[0]

    if namespace is None:
        store = get_config_store()
        pods = [p for p in pods if not store.is_namespace_excluded(p["namespace"])]
        deployments = [d for d in deployments if not store.is_namespace_excluded(d["namespace"])]
        statefulsets = [s for s in statefulsets if not store.is_namespace_excluded(s["namespace"])]
        daemonsets = [ds for ds in daemonsets if not store.is_namespace_excluded(ds["namespace"])]
        services = [s for s in services if not store.is_namespace_excluded(s["namespace"])]
        configmaps = [cm for cm in configmaps if not store.is_namespace_excluded(cm["namespace"])]
        raw_pod_items = [
            rp for rp in raw_pod_items
            if not store.is_namespace_excluded(rp.get("metadata", {}).get("namespace", ""))
        ]
        raw_rs_items = [
            rs for rs in raw_rs_items
            if not store.is_namespace_excluded(rs.get("metadata", {}).get("namespace", ""))
        ]

    raw_pod_by_name = {rp.get("metadata", {}).get("name"): rp for rp in raw_pod_items}
    findings: List[Dict[str, Any]] = []

    # -- STEP 2: kube-apiserver {authn} (ServiceAccount token disabled or missing) --
    for rp in raw_pod_items:
        meta = rp.get("metadata", {}) or {}
        spec = rp.get("spec", {}) or {}
        if spec.get("automountServiceAccountToken") is False and (meta.get("labels") or {}).get("kubeorbit.io/needs-api-auth") == "true":
            p_name = meta.get("name", "")
            p_ns = meta.get("namespace", "default")
            owner = _owner_label(rp)
            findings.append({
                "kind": "active_failure",
                "namespace": p_ns,
                "step_number": 2,
                "stage_id": "steps-1-6",
                "step_badge": "Step 2 · kube-apiserver {authn}",
                "component": "kube-apiserver · authn",
                "component_id": "kube-apiserver",
                "issue_type": "Step 2: ServiceAccount Token Automount Disabled (401 Risk)",
                "severity": "warning",
                "confidence": 0.95,
                "root_cause": (
                    f"[Step 2 · kube-apiserver {{authn}}] Pod '{p_name}' sets automountServiceAccountToken=false while labeled "
                    "for in-cluster API calls, causing 401 Unauthorized responses from kube-apiserver."
                ),
                "remediation_cmd": f"kubectl patch {owner.lower()} -n {p_ns} -p '{{\"spec\":{{\"template\":{{\"spec\":{{\"automountServiceAccountToken\":true}}}}}}}}'",
                "resource_name": p_name,
                "impacted_service": owner.split("/", 1)[-1],
            })

    # -- STEP 3: kube-apiserver {rbac-authz} (Forbidden events in cluster) --
    for ev in events:
        msg = ev.get("message") or ""
        if "forbidden:" in msg.lower() and "podsecurity" not in msg.lower() and "quota" not in msg.lower():
            ev_ns = ev.get("namespace") or "default"
            inv = ev.get("involved_name") or "resource"
            findings.append({
                "kind": "active_failure",
                "namespace": ev_ns,
                "step_number": 3,
                "stage_id": "steps-1-6",
                "step_badge": "Step 3 · kube-apiserver {rbac-authz}",
                "component": "kube-apiserver · rbac-authz",
                "component_id": "kube-apiserver",
                "issue_type": "Step 3: RBAC Authorization Forbidden",
                "severity": "critical",
                "confidence": 0.98,
                "root_cause": f"[Step 3 · kube-apiserver {{rbac-authz}}] Authorization denied by RBAC policy: {msg[:220]}",
                "remediation_cmd": f"kubectl get rolebindings,clusterrolebindings -n {ev_ns}",
                "resource_name": inv,
                "impacted_service": inv,
            })

    # -- STEP 7: kube-controller-manager {deployment-controller} (Rollout stalled) --
    for d in deployments:
        if d.get("desired", 0) > 0 and d.get("ready", 0) < d.get("desired", 0):
            d_name = d["name"]
            d_ns = d["namespace"]
            findings.append({
                "kind": "active_failure",
                "namespace": d_ns,
                "step_number": 7,
                "stage_id": "steps-7-8",
                "step_badge": "Step 7 · {deployment-controller}",
                "component": "kube-controller-manager",
                "component_id": "kube-controller-manager",
                "issue_type": "Step 7: Deployment Rollout Degraded",
                "severity": "warning",
                "confidence": 0.94,
                "root_cause": (
                    f"[Step 7 · {{deployment-controller}}] Deployment '{d_name}' has {d.get('ready', 0)}/{d.get('desired', 0)} "
                    "ready replicas; deployment-controller rollout has not converged."
                ),
                "remediation_cmd": f"kubectl rollout status deployment/{d_name} -n {d_ns} && kubectl describe deployment/{d_name} -n {d_ns}",
                "resource_name": f"Deployment/{d_name}",
                "impacted_service": d_name,
            })

    # -- STEP 8: ReplicaSet Controller / PodSecurity / ResourceQuota Failures --
    for rs in raw_rs_items:
        meta = rs.get("metadata", {}) or {}
        rs_name = meta.get("name", "")
        rs_ns = meta.get("namespace", "default")
        desired = (rs.get("spec") or {}).get("replicas", 0) or 0
        current = (rs.get("status") or {}).get("replicas", 0) or 0
        if desired > 0 and current < desired:
            rs_events = [
                e for e in events
                if e.get("involved_name") == rs_name and e.get("reason") in ("FailedCreate", "Failed")
            ]
            conds = (rs.get("status") or {}).get("conditions") or []
            replica_failure = next((c for c in conds if c.get("type") == "ReplicaFailure"), None)
            if rs_events or replica_failure:
                msg = (
                    rs_events[0].get("message")
                    if rs_events
                    else (replica_failure or {}).get("message", "ReplicaSet failed to create Pod objects.")
                )
                owner = _owner_label(rs)
                findings.append({
                    "kind": "active_failure",
                    "namespace": rs_ns,
                    "step_number": 8,
                    "stage_id": "steps-7-8",
                    "step_badge": "Step 8 · ReplicaSet / PodSecurity",
                    "component": "kube-controller-manager",
                    "component_id": "kube-controller-manager",
                    "issue_type": "Step 8: Pod Creation Blocked (FailedCreate)",
                    "severity": "critical",
                    "confidence": 0.98,
                    "root_cause": (
                        f"ReplicaSet '{rs_name}' desires {desired} pod(s) but created {current}. "
                        f"API server rejected Pod creation at Step 8 (PodSecurity Admission, ResourceQuota, or missing ServiceAccount): {msg}"
                    ),
                    "remediation_cmd": f"kubectl describe rs {rs_name} -n {rs_ns}",
                    "resource_name": rs_name,
                    "impacted_service": owner.split("/", 1)[-1],
                })

    # -- STEPS 9, 10, 11: Active Pod-Level Failures ----------------------------
    failing_candidates = []
    for p in pods:
        p_name = p["name"]
        p_ns = p["namespace"]
        status = p["status"]
        restarts = p.get("restarts", 0)
        raw_pod = raw_pod_by_name.get(p_name)
        conds = _pod_conditions_map(raw_pod)
        wait_list = _waiting_reasons(raw_pod)
        wait_reasons = {w[1] for w in wait_list}
        node_name = (raw_pod or {}).get("spec", {}).get("nodeName")
        phase = (raw_pod or {}).get("status", {}).get("phase", status)

        pod_events = [
            e for e in events
            if e.get("involved_name") == p_name or p_name in (e.get("message") or "")
        ]
        event_reasons = {e.get("reason", "") for e in pod_events}
        event_text = " | ".join(e.get("message", "") for e in pod_events[:5])

        is_issue = False
        issue_type = None
        severity = "critical"
        step_num = 10
        stage_id = "step-10"
        step_badge = "Step 10 · kubelet"
        component = "kubelet · worker node"
        component_id = "kubelet-runtime"

        # Step 9: kube-scheduler (Pending with no node bound or FailedScheduling)
        if (
            status == "Pending"
            and (
                not node_name
                or (conds.get("PodScheduled") or {}).get("status") == "False"
                or "FailedScheduling" in event_reasons
            )
        ):
            is_issue = True
            step_num = 9
            stage_id = "step-9"
            component = "kube-scheduler"
            component_id = "kube-scheduler"
            severity = "warning"
            sched_msg = (conds.get("PodScheduled") or {}).get("message", "") or event_text
            if "persistentvolumeclaim" in sched_msg.lower() or "unbound" in sched_msg.lower():
                issue_type = "Step 9: Scheduler Stalled (Unbound PVC)"
                step_badge = "Step 9 · kube-scheduler (PVC)"
            elif "insufficient cpu" in sched_msg.lower() or "insufficient memory" in sched_msg.lower():
                issue_type = "Step 9: Scheduler Stalled (Insufficient CPU/Memory)"
                step_badge = "Step 9 · kube-scheduler (Capacity)"
            elif "taint" in sched_msg.lower():
                issue_type = "Step 9: Scheduler Stalled (Untolerated Node Taint)"
                step_badge = "Step 9 · kube-scheduler (Taints)"
            elif "affinity" in sched_msg.lower() or "node selector" in sched_msg.lower() or "nodeselector" in sched_msg.lower():
                issue_type = "Step 9: Scheduler Stalled (Node Selector / Affinity)"
                step_badge = "Step 9 · kube-scheduler (Affinity)"
            else:
                issue_type = "Step 9: Scheduler Stalled (Pending / Unschedulable)"
                step_badge = "Step 9 · kube-scheduler"

        # Step 10a: kubelet CSI Volume Mount / Attach failure
        elif (
            "FailedMount" in event_reasons
            or "FailedAttachVolume" in event_reasons
            or (status in ("Pending", "ContainerCreating") and ("FailedMount" in event_text or "volume" in event_text.lower()))
        ):
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10a · kubelet CSI Mount"
            component = "kubelet · CSI volume plugin"
            component_id = "kubelet-runtime"
            issue_type = "Step 10a: CSI Volume / Secret Mount Failure"
            severity = "critical"

        # Step 10b: kubelet CNI Network Sandbox failure
        elif "FailedCreatePodSandBox" in event_reasons or "NetworkNotReady" in event_reasons:
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10b · kubelet CNI Sandbox"
            component = "kubelet · CNI network plugin"
            component_id = "kubelet-runtime"
            issue_type = "Step 10b: CNI Network Sandbox Failure"
            severity = "critical"

        # Step 10c: kubelet Image Pull failure
        elif (
            "ImagePullBackOff" in status
            or "ErrImagePull" in status
            or "ImagePullBackOff" in wait_reasons
            or "ErrImagePull" in wait_reasons
        ):
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10c · kubelet Image Pull"
            component = "kubelet · image registry"
            component_id = "kubelet-runtime"
            issue_type = "Step 10c: Container Image Pull Failure"
            severity = "critical"

        # Step 10d: CreateContainerConfigError / Init failure / CrashLoopBackOff / OOMKilled
        elif (
            "CreateContainerConfigError" in status
            or "CreateContainerConfigError" in wait_reasons
            or "CreateContainerError" in wait_reasons
        ):
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10d · kubelet Container Config"
            component = "kubelet · containerd"
            component_id = "kubelet-runtime"
            issue_type = "Step 10d: Missing Secret/ConfigMap Key (CreateContainerConfigError)"
            severity = "critical"
        elif status.startswith("Init:") or "Init" in status:
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10d · kubelet InitContainer"
            component = "kubelet · initContainers"
            component_id = "kubelet-runtime"
            issue_type = "Step 10d: InitContainer Execution Failure"
            severity = "critical"
        elif "CrashLoopBackOff" in status or "Error" in status or "CrashLoopBackOff" in wait_reasons:
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10d · kubelet Runtime (containerd)"
            component = "kubelet · containerd"
            component_id = "kubelet-runtime"
            issue_type = "Step 10d: Container CrashLoopBackOff"
            severity = "critical"
        elif "OOMKilled" in status or restarts >= 3:
            is_issue = True
            step_num = 10
            stage_id = "step-10"
            step_badge = "Step 10d · kubelet OOMKilled"
            component = "kubelet · cgroups / containerd"
            component_id = "kubelet-runtime"
            issue_type = "Step 10d: Frequent Restarts / OOMKilled"
            severity = "critical"

        # Step 11: Running container, but Readiness / Startup Probe failing (0/N Ready)
        elif (
            phase == "Running"
            and (
                (conds.get("Ready") or {}).get("status") == "False"
                or "Unhealthy" in event_reasons
                or (isinstance(p.get("ready"), str) and p["ready"].startswith("0/"))
            )
        ):
            is_issue = True
            step_num = 11
            stage_id = "step-11"
            step_badge = "Step 11 · kubelet Readiness Probe"
            component = "kubelet · readinessProbe"
            component_id = "kubelet-probes"
            issue_type = "Step 11: Readiness Probe Failing (Pod Not Ready)"
            severity = "warning"

        # Fallback for any other Pending / ContainerCreating pod
        elif status in ("Pending", "ContainerCreating"):
            is_issue = True
            step_num = 10 if node_name else 9
            stage_id = "step-10" if node_name else "step-9"
            step_badge = "Step 10 · kubelet Startup" if node_name else "Step 9 · kube-scheduler"
            component = "kubelet" if node_name else "kube-scheduler"
            component_id = "kubelet-runtime" if node_name else "kube-scheduler"
            issue_type = "Step 10: ContainerCreating Stalled" if node_name else "Step 9: Scheduler Stalled (Pending)"
            severity = "warning"

        if is_issue:
            failing_candidates.append((p, issue_type, severity, step_num, stage_id, step_badge, component, component_id))

    async def _fetch_snippet(p_ns: str, p_name: str, fallback: str, should_fetch: bool) -> str:
        if not should_fetch:
            return fallback
        try:
            return await get_pod_logs(context, kubeconfig, p_ns, p_name, tail_lines=20)
        except Exception:
            return fallback

    log_tasks = []
    for idx, (p, issue_type, severity, step_num, stage_id, step_badge, component, component_id) in enumerate(failing_candidates):
        p_name = p["name"]
        p_ns = p["namespace"]
        pod_events = [
            e for e in events
            if e.get("involved_name") == p_name or p_name in (e.get("message") or "")
        ]
        latest_msg = pod_events[0]["message"] if pod_events else "Container or pod lifecycle check failed"
        should_fetch_logs = idx < _MAX_LOG_FETCH_PODS and stage_id == "step-10" and "10d" in step_badge
        log_tasks.append(_fetch_snippet(p_ns, p_name, latest_msg, should_fetch_logs))

    snippets = await asyncio.gather(*log_tasks) if log_tasks else []

    for (p, issue_type, severity, step_num, stage_id, step_badge, component, component_id), pod_logs_snippet in zip(failing_candidates, snippets):
        p_name = p["name"]
        p_ns = p["namespace"]
        status = p["status"]
        restarts = p.get("restarts", 0)
        raw_pod = raw_pod_by_name.get(p_name)
        conds = _pod_conditions_map(raw_pod)
        wait_list = _waiting_reasons(raw_pod)
        pod_events = [
            e for e in events
            if e.get("involved_name") == p_name or p_name in (e.get("message") or "")
        ]
        latest_msg = pod_events[0]["message"] if pod_events else ""
        if not latest_msg and wait_list:
            latest_msg = f"{wait_list[0][1]}: {wait_list[0][2]}"
        if not latest_msg and conds.get("PodScheduled", {}).get("message"):
            latest_msg = conds["PodScheduled"]["message"]

        owner_label = _owner_label(raw_pod) if raw_pod else f"Pod/{p_name}"
        owner_short = owner_label.split("/", 1)[-1]

        root_cause = f"[{step_badge}] {latest_msg or 'Workload stalled during lifecycle progression.'}"
        remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns}"
        confidence = 0.92

        if stage_id == "step-9":
            sched_detail = (conds.get("PodScheduled") or {}).get("message") or latest_msg or "No nodes matched scheduling predicates."
            root_cause = (
                f"[Step 9 · kube-scheduler] Pod '{p_name}' is Pending with no node assigned (filter → score → bind stalled). "
                f"Scheduler message: {sched_detail}"
            )
            if "Unbound PVC" in issue_type:
                remediation_cmd = f"kubectl get pvc -n {p_ns} && kubectl describe pod {p_name} -n {p_ns}"
            else:
                remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 12 Events"
            confidence = 0.96
        elif "10a" in step_badge:
            root_cause = (
                f"[Step 10a · kubelet CSI Mount] Pod '{p_name}' is assigned to node '{(raw_pod or {}).get('spec', {}).get('nodeName', 'node')}' "
                f"but kubelet cannot mount a referenced volume/ConfigMap/Secret: {latest_msg}"
            )
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 10 'FailedMount\\|Volumes'"
            confidence = 0.97
        elif "10b" in step_badge:
            root_cause = (
                f"[Step 10b · kubelet CNI Sandbox] kubelet failed to allocate pod network namespace or IP via CNI plugin: {latest_msg}"
            )
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 10 FailedCreatePodSandBox"
            confidence = 0.96
        elif "10c" in step_badge:
            root_cause = (
                f"[Step 10c · kubelet Image Pull] Container image registry returned 404/unauthorized or tag does not exist. "
                f"Detail: {latest_msg}"
            )
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 8 'Failed to pull image\\|Back-off pulling'"
            confidence = 0.99
        elif "CreateContainerConfigError" in issue_type:
            root_cause = (
                f"[Step 10d · kubelet Container Config] Container cannot start because a referenced Secret or ConfigMap key is missing: {latest_msg}"
            )
            remediation_cmd = f"kubectl get secret,configmap -n {p_ns} && kubectl describe pod {p_name} -n {p_ns}"
            confidence = 0.99
        elif "OOM" in pod_logs_snippet or "memory" in pod_logs_snippet.lower() or "OOMKilled" in status:
            step_badge = "Step 10d · kubelet OOMKilled"
            root_cause = (
                f"[Step 10d · kubelet Container Runtime] Out of Memory (OOMKilled / exit 137) — container memory limit exceeded. "
                f"Log snippet: '{pod_logs_snippet[:120].strip()}'"
            )
            remediation_cmd = f"kubectl set resources {owner_label.lower()} -n {p_ns} --limits=memory=256Mi"
            confidence = 0.98
        elif "10d" in step_badge:
            root_cause = (
                f"[Step 10d · kubelet Container Runtime] Process terminated with non-zero exit code. "
                f"Log snippet: '{pod_logs_snippet[:140].strip()}'"
            )
            remediation_cmd = f"kubectl logs {p_name} -n {p_ns} --previous"
            confidence = 0.93
        elif stage_id == "step-11":
            unhealthy_ev = next((e["message"] for e in pod_events if e.get("reason") == "Unhealthy"), latest_msg)
            root_cause = (
                f"[Step 11 · kubelet Readiness Probe] Container is Running on the node, but readinessProbe/startupProbe is failing "
                f"so the Pod is excluded from Service EndpointSlices (0 traffic routed). Detail: {unhealthy_ev or 'Readiness condition is False.'}"
            )
            remediation_cmd = f"kubectl describe pod {p_name} -n {p_ns} | grep -A 10 'Readiness\\|Unhealthy'"
            confidence = 0.97

        findings.append({
            "kind": "active_failure",
            "namespace": p_ns,
            "step_number": step_num,
            "stage_id": stage_id,
            "step_badge": step_badge,
            "component": component,
            "component_id": component_id,
            "issue_type": issue_type,
            "severity": severity,
            "confidence": confidence,
            "root_cause": root_cause,
            "remediation_cmd": remediation_cmd,
            "resource_name": p_name,
            "impacted_service": owner_short,
            "sample": {
                "resource_type": "Pod",
                "resource_name": p_name,
                "namespace": p_ns,
                "status": status,
                "restarts": restarts,
                "latest_event": latest_msg,
                "log_snippet": pod_logs_snippet[-300:] if pod_logs_snippet else "",
            },
        })

    # -- STEP 12: EndpointSlice / Service Selector -> 0 Ready Endpoints (503 Risk) --
    for svc in services:
        svc_name = svc.get("name", "")
        svc_ns = svc.get("namespace", "default")
        if svc_name == "kubernetes" and svc_ns == "default":
            continue
        selector = svc.get("selector") or {}
        if not selector:
            continue

        ns_raw_pods = [
            rp for rp in raw_pod_items
            if rp.get("metadata", {}).get("namespace") == svc_ns
        ]
        matching_pods = []
        ready_matching_pods = []
        for rp in ns_raw_pods:
            p_labels = rp.get("metadata", {}).get("labels") or {}
            if all(p_labels.get(k) == v for k, v in selector.items()):
                matching_pods.append(rp)
                conds = _pod_conditions_map(rp)
                if (conds.get("Ready") or {}).get("status") == "True":
                    ready_matching_pods.append(rp)

        sel_str = ",".join(f"{k}={v}" for k, v in selector.items())
        if len(matching_pods) == 0:
            findings.append({
                "kind": "active_failure",
                "namespace": svc_ns,
                "step_number": 12,
                "stage_id": "step-12",
                "step_badge": "Step 12 · EndpointSlice (Selector Mismatch)",
                "component": "endpointslice-controller / Service",
                "component_id": "datapath-routing",
                "issue_type": "Step 12: Service Selector Matches 0 Pods (503 Risk)",
                "severity": "critical",
                "confidence": 0.99,
                "root_cause": (
                    f"[Step 12 · EndpointSlice Controller] Service '{svc_name}' has selector '{sel_str}', "
                    f"which matches 0 pods in namespace '{svc_ns}'. Its EndpointSlice has 0 addresses, causing immediate HTTP 503s from Ingress/LoadBalancers."
                ),
                "remediation_cmd": f"kubectl get pods -n {svc_ns} --show-labels && kubectl describe svc {svc_name} -n {svc_ns}",
                "resource_name": f"Service/{svc_name}",
                "impacted_service": svc_name,
            })
        elif len(ready_matching_pods) == 0:
            findings.append({
                "kind": "active_failure",
                "namespace": svc_ns,
                "step_number": 12,
                "stage_id": "step-12",
                "step_badge": "Step 12 · EndpointSlice (0 Ready Endpoints)",
                "component": "endpointslice-controller / kube-proxy",
                "component_id": "datapath-routing",
                "issue_type": "Step 12: Service Has 0 Ready Endpoints (Traffic Blocked)",
                "severity": "warning",
                "confidence": 0.97,
                "root_cause": (
                    f"[Step 12 · EndpointSlice Controller] Service '{svc_name}' matches {len(matching_pods)} pod(s) via '{sel_str}', "
                    f"but 0 pods are Ready. endpointslice-controller has removed all backend IPs — traffic to '{svc_name}' will fail with 503."
                ),
                "remediation_cmd": f"kubectl get endpointslices -l kubernetes.io/service-name={svc_name} -n {svc_ns}",
                "resource_name": f"Service/{svc_name}",
                "impacted_service": svc_name,
            })

    # -- 2. Proactive best-practice checks (run regardless of failures) -----
    for pod in raw_pod_items:
        meta = pod.get("metadata", {})
        p_ns, _ = meta.get("namespace"), meta.get("name")
        owner = _owner_label(pod)
        containers = pod.get("spec", {}).get("containers", []) or []

        for c in containers:
            resources = c.get("resources", {}) or {}
            if not resources.get("requests") and not resources.get("limits"):
                findings.append({
                    "kind": "best_practice",
                    "namespace": p_ns,
                    "step_number": 4,
                    "stage_id": "steps-1-6",
                    "step_badge": "Step 4 & 9 · Best Practice",
                    "component": "kube-apiserver / kube-scheduler",
                    "component_id": "kube-apiserver",
                    "issue_type": "Missing Resource Requests/Limits",
                    "severity": "warning",
                    "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} has no CPU/memory requests or limits set, "
                                  "so LimitRange defaults (Step 4) and kube-scheduler (Step 9) can't bound its footprint.",
                    "remediation_cmd": f"kubectl set resources {owner.lower()} -n {p_ns} --requests=cpu=100m,memory=128Mi --limits=cpu=500m,memory=512Mi",
                    "resource_name": owner,
                    "impacted_service": owner,
                })

            if not c.get("livenessProbe") and not c.get("readinessProbe"):
                findings.append({
                    "kind": "best_practice",
                    "namespace": p_ns,
                    "step_number": 11,
                    "stage_id": "step-11",
                    "step_badge": "Step 11 · Best Practice",
                    "component": "kubelet · probes",
                    "component_id": "kubelet-probes",
                    "issue_type": "No Liveness/Readiness Probes",
                    "severity": "warning",
                    "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} has neither a liveness nor a readiness probe, "
                                  "so Step 11 (readiness gate) is bypassed and EndpointSlice (Step 12) routes traffic before the app is ready.",
                    "remediation_cmd": f"kubectl edit {owner.lower()} -n {p_ns}  # add livenessProbe/readinessProbe",
                    "resource_name": owner,
                    "impacted_service": owner,
                })

            image = c.get("image", "") or ""
            if image.endswith(":latest") or (":" not in image.split("/")[-1] and "@" not in image):
                findings.append({
                    "kind": "best_practice",
                    "namespace": p_ns,
                    "step_number": 10,
                    "stage_id": "step-10",
                    "step_badge": "Step 10c · Best Practice",
                    "component": "kubelet · image pull",
                    "component_id": "kubelet-runtime",
                    "issue_type": "Untagged / :latest Image",
                    "severity": "info",
                    "confidence": 1.0,
                    "root_cause": f"Container '{c.get('name')}' in {owner} runs '{image}', which is not reproducible — "
                                  "a redeploy at Step 10c can silently pull a different image than what's running now.",
                    "remediation_cmd": f"kubectl set image {owner.lower()} -n {p_ns} {c.get('name')}={image.split(':')[0]}:<pinned-version>",
                    "resource_name": owner,
                    "impacted_service": owner,
                })

    for w in [*deployments, *statefulsets]:
        if w.get("desired") == 1:
            owner = w["name"]
            findings.append({
                "kind": "best_practice",
                "namespace": w["namespace"],
                "step_number": 7,
                "stage_id": "steps-7-8",
                "step_badge": "Step 7–8 · Best Practice",
                "component": "kube-controller-manager",
                "component_id": "kube-controller-manager",
                "issue_type": "Single-Replica Workload",
                "severity": "info",
                "confidence": 1.0,
                "root_cause": f"'{owner}' runs a single replica — a node drain, OOM, or crash causes full downtime "
                              "for it with no rolling-update headroom.",
                "remediation_cmd": f"kubectl scale deployment/{owner} -n {w['namespace']} --replicas=2",
                "resource_name": owner,
                "impacted_service": owner,
            })

    namespaces_with_workloads = sorted({w["namespace"] for w in [*deployments, *statefulsets, *daemonsets]})
    netpol_results = await asyncio.gather(*(
        cached_kubectl_list(context, kubeconfig, "networkpolicies", check_ns, ttl, optional=True)
        for check_ns in namespaces_with_workloads
    )) if namespaces_with_workloads else []

    for check_ns, (netpols, available) in zip(namespaces_with_workloads, netpol_results):
        if available and not netpols:
            findings.append({
                "kind": "best_practice",
                "namespace": check_ns,
                "step_number": 12,
                "stage_id": "step-12",
                "step_badge": "Step 12 · Best Practice",
                "component": "CNI NetworkPolicy",
                "component_id": "datapath-routing",
                "issue_type": "No NetworkPolicy in Namespace",
                "severity": "info",
                "confidence": 1.0,
                "root_cause": f"Namespace '{check_ns}' has workloads but zero NetworkPolicies — every pod can reach "
                              "every other pod cluster-wide by default (no network segmentation).",
                "remediation_cmd": f"kubectl get networkpolicy -n {check_ns}  # then apply a default-deny baseline",
                "resource_name": check_ns,
                "impacted_service": check_ns,
            })

    all_workloads = [*deployments, *statefulsets, *daemonsets]
    workloads_healthy = sum(1 for w in all_workloads if w.get("healthy", True))

    return _build_rca_payload(
        findings=findings,
        workloads_healthy=workloads_healthy,
        workloads_total=len(all_workloads),
        namespaces_with_workloads=namespaces_with_workloads,
        simulated=False,
    )
