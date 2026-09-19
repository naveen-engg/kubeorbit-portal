"""The complete, intentionally short list of read-only KubeOrbit capabilities
exposed to an MCP client.

Every function here is a 1:1 wrapper around one existing GET route in
app.py -- nothing here talks to kubectl/kube_client directly. That means
these tools inherit KubeOrbit's own cluster auth, TTL caching, and (where
applicable) audit logging for free, and it keeps this file reviewable as a
whole: the entire read-only surface an agent gets is exactly the functions
defined below, in one file.

Deliberately NOT exposed as tools, even though KubeOrbit's API has them:
  - POST .../secrets/{name}/reveal   (decodes real secret values)
  - POST .../workloads/.../rollback  (mutates a live workload)
  - POST/DELETE /api/admin/clusters/*  (adds/removes cluster registrations)
  - POST .../clusters/{id}/switch    (not needed -- every route below
    resolves its own cluster context from cluster_id, see app.py's
    get_cluster_context dependency)
If a future change to this file adds a tool backed by anything other than a
GET request, that is a deliberate scope change to "read-only access" and
should be treated as one.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .client import get_client


async def list_teams_and_clusters() -> Any:
    """List every team -> environment -> cluster in KubeOrbit's registry
    (no credentials or secrets -- just ids/names/providers), the starting
    point for picking which `cluster_id` to use in every other tool."""
    return await get_client().get("/api/teams")


async def get_fleet_overview() -> Any:
    """Unified multi-cluster dashboard: node/pod counts and health rolled up
    across every registered cluster in one call."""
    return await get_client().get("/api/fleet/overview")


async def get_cluster_info(cluster_id: str) -> Any:
    """Kubernetes version, node pools, console links, and Helm release count
    for one cluster."""
    return await get_client().get(f"/api/clusters/{cluster_id}/info")


async def get_nodes(cluster_id: str) -> Any:
    """List every node in the cluster with readiness, zone, node pool, and capacity."""
    return await get_client().get(f"/api/clusters/{cluster_id}/nodes")


async def get_namespaces(cluster_id: str, include_system: bool = False) -> Any:
    """List namespaces. System namespaces (kube-system etc.) are excluded by
    default, matching the KubeOrbit UI's default view."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces", {"include_system": include_system}
    )


async def get_workloads_overview(cluster_id: str, namespace: Optional[str] = None) -> Any:
    """Health rollup (healthy/unhealthy counts) for pods, deployments,
    statefulsets, daemonsets, jobs, and cronjobs. Omit namespace for the
    whole cluster."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads", {"namespace": namespace}
    )


async def get_namespace_resources(cluster_id: str, namespace: str) -> Any:
    """Everything in one namespace in a single call: pods, services,
    configmaps, secret *metadata only* (never decoded values), gateways,
    httproutes, pvcs, and serviceaccounts."""
    return await get_client().get(f"/api/clusters/{cluster_id}/namespaces/{namespace}/resources")


async def get_workloads_catalog(cluster_id: str, namespace: Optional[str] = None) -> Any:
    """Full catalog of deployments/statefulsets/daemonsets/jobs/cronjobs with
    a health summary."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/catalog", {"namespace": namespace}
    )


async def get_rollout_history(cluster_id: str, namespace: str, workload_type: str, name: str) -> Any:
    """Revision history (image, change-cause per revision) for a Deployment,
    StatefulSet, or DaemonSet. Read-only -- does not roll back anything."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollout/history",
        {"namespace": namespace},
    )


async def get_applications(cluster_id: str, namespace: Optional[str] = None) -> Any:
    """Workloads/services grouped into logical applications (by
    app.kubernetes.io labels or name prefix)."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/applications", {"namespace": namespace}
    )


async def get_pod_details(cluster_id: str, namespace: str, pod_name: str) -> Any:
    """Deep single-pod inspection: container probe configuration, restart
    reasons, resource requests/limits, and recent log tail."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/details"
    )


async def get_pod_logs(
    cluster_id: str, namespace: str, pod_name: str, container: Optional[str] = None, tail_lines: int = 200
) -> Any:
    """Tail a pod's logs (optionally a specific container)."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/logs",
        {"container": container, "tail_lines": tail_lines},
    )


async def get_manifest(cluster_id: str, kind: str, name: str, namespace: Optional[str] = None) -> Any:
    """Raw `kubectl get -o yaml` for one resource -- read-only, does not
    apply or modify anything."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/manifest", {"kind": kind, "name": name, "namespace": namespace}
    )


async def get_network_topology(cluster_id: str, namespace: str) -> Any:
    """ILB -> Gateway -> HTTPRoute -> Service -> Pod graph for one namespace."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/network", {"namespace": namespace}
    )


async def get_storage_topology(cluster_id: str, namespace: str) -> Any:
    """StorageClass -> PV -> PVC -> Pod graph for one namespace."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/storage", {"namespace": namespace}
    )


async def get_namespace_map(cluster_id: str, namespace: str) -> Any:
    """The full namespace resource graph: routing in through every workload,
    pod, and the ConfigMaps/Secrets/ServiceAccount/PVCs each pod actually
    references. The richest single topology view -- prefer this one unless
    you specifically need the narrower network/storage-only graphs above."""
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/map", {"namespace": namespace})


async def get_cluster_workload_map(cluster_id: str) -> Any:
    """Cluster-wide node -> pod placement map with per-node CPU/memory usage."""
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/clustermap")


async def get_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 100) -> Any:
    """Recent Kubernetes events, newest first. Omit namespace for cluster-wide."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events", {"namespace": namespace, "limit": limit}
    )


async def get_extended_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 200) -> Any:
    """Live events merged with a longer-lived in-memory history, plus
    warning/normal counts -- useful for events the API server has already
    garbage-collected."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events/extended", {"namespace": namespace, "limit": limit}
    )


async def get_helm_releases(cluster_id: str) -> Any:
    """List installed Helm releases on the cluster."""
    return await get_client().get(f"/api/clusters/{cluster_id}/helm")


async def get_cost_estimate(cluster_id: str) -> Any:
    """Estimated monthly compute cost per node pool from each cloud's public
    on-demand list pricing (AKS/EKS/GKE only -- not available for local
    clusters). Always carries a disclaimer: this is a list-price estimate,
    not the customer's actual invoice (excludes discounts, storage, egress,
    load balancers, control-plane fees)."""
    return await get_client().get(f"/api/clusters/{cluster_id}/cost")


async def get_ecosystem_overview(cluster_id: str) -> Any:
    """Which well-known platform CRDs (cert-manager, KEDA, Karpenter, Velero,
    External Secrets, Kyverno, Argo CD, Flux, ...) are installed on this
    cluster, with resource counts per category."""
    return await get_client().get(f"/api/clusters/{cluster_id}/ecosystem")


async def get_audit_logs(cluster_id: Optional[str] = None, limit: int = 50) -> Any:
    """KubeOrbit's own audit trail of privileged actions (secret reveals,
    rollbacks) taken through the portal -- read-only visibility into who did
    what, not a way to take those actions."""
    return await get_client().get("/api/audit/logs", {"cluster_id": cluster_id, "limit": limit})


async def diagnose_cluster_issues(cluster_id: str, namespace: Optional[str] = None) -> Any:
    """Run KubeOrbit's root-cause-analysis scan: degraded pods folded into
    grouped issues with a likely root cause, confidence, and a suggested
    (not executed) kubectl remediation command, plus an overall health score."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/diagnostics/rca", {"namespace": namespace}
    )
