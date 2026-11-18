"""The complete, intentionally short list of read-only KubeOrbit capabilities
exposed to an MCP client and the in-app Google ADK agent.

Every function here is a 1:1 wrapper around one existing GET route in
app.py -- nothing here talks to kubectl/kube_client directly. That means
these tools inherit KubeOrbit's own cluster auth, TTL caching, and (where
applicable) audit logging for free, and it keeps this file reviewable as a
whole: the entire read-only surface an agent gets is exactly the functions
defined below, in one file.

NOTE:
1. Do not add `from __future__ import annotations` in this module: FastMCP
   inspects parameter annotations at runtime via `issubclass(param.annotation, ...)`.
2. Return types are annotated as `dict` (not `Any`) so Google GenAI / ADK's
   automatic function-calling schema builder generates clean OpenAPI object schemas.
"""
from typing import Optional

from .client import get_client


async def list_teams_and_clusters() -> dict:
    """List every team -> environment -> cluster in KubeOrbit's registry
    (no credentials or secrets -- just ids/names/providers), the starting
    point for picking which `cluster_id` to use in every other tool."""
    return await get_client().get("/api/teams")


async def get_fleet_overview() -> dict:
    """Unified multi-cluster dashboard: node/pod counts and health rolled up
    across every registered cluster in one call."""
    return await get_client().get("/api/fleet/overview")


async def get_cluster_info(cluster_id: str) -> dict:
    """Kubernetes version, node pools, console links, and Helm release count
    for one cluster."""
    return await get_client().get(f"/api/clusters/{cluster_id}/info")


async def get_nodes(cluster_id: str) -> dict:
    """List every node in the cluster with readiness, zone, node pool, and capacity."""
    return await get_client().get(f"/api/clusters/{cluster_id}/nodes")


async def get_namespaces(cluster_id: str, include_system: bool = False) -> dict:
    """List namespaces. System namespaces (kube-system etc.) are excluded by
    default, matching the KubeOrbit UI's default view."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces", {"include_system": include_system}
    )


async def get_workloads_overview(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Health rollup (healthy/unhealthy counts) for pods, deployments,
    statefulsets, daemonsets, jobs, and cronjobs. Omit namespace for the
    whole cluster."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads", {"namespace": namespace}
    )


async def get_namespace_resources(cluster_id: str, namespace: str) -> dict:
    """Everything in one namespace in a single call: pods, services,
    configmaps, secret *metadata only* (never decoded values), gateways,
    httproutes, pvcs, and serviceaccounts."""
    return await get_client().get(f"/api/clusters/{cluster_id}/namespaces/{namespace}/resources")


async def get_workloads_catalog(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Full catalog of deployments/statefulsets/daemonsets/jobs/cronjobs with
    a health summary."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/catalog", {"namespace": namespace}
    )


async def get_rollout_history(cluster_id: str, namespace: str, workload_type: str, name: str) -> dict:
    """Revision history (image, change-cause per revision) for a Deployment,
    StatefulSet, or DaemonSet. Read-only -- does not roll back anything."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollout/history",
        {"namespace": namespace},
    )


async def get_applications(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Workloads/services grouped into logical applications (by
    app.kubernetes.io labels or name prefix)."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/applications", {"namespace": namespace}
    )


async def get_pod_details(cluster_id: str, namespace: str, pod_name: str) -> dict:
    """Deep single-pod inspection: container probe configuration, restart
    reasons, resource requests/limits, and recent log tail."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/details"
    )


async def get_pod_logs(
    cluster_id: str, namespace: str, pod_name: str, container: Optional[str] = None, tail_lines: int = 200
) -> dict:
    """Tail a pod's logs (optionally a specific container)."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/logs",
        {"container": container, "tail_lines": tail_lines},
    )


async def get_manifest(cluster_id: str, kind: str, name: str, namespace: Optional[str] = None) -> dict:
    """Raw `kubectl get -o yaml` for one resource -- read-only, does not
    apply or modify anything."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/manifest", {"kind": kind, "name": name, "namespace": namespace}
    )


async def get_network_topology(cluster_id: str, namespace: str) -> dict:
    """ILB -> Gateway -> HTTPRoute -> Service -> Pod graph for one namespace."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/network", {"namespace": namespace}
    )


async def get_storage_topology(cluster_id: str, namespace: str) -> dict:
    """StorageClass -> PV -> PVC -> Pod graph for one namespace."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/storage", {"namespace": namespace}
    )


async def get_namespace_map(cluster_id: str, namespace: str) -> dict:
    """The full namespace resource graph: routing in through every workload,
    pod, and the ConfigMaps/Secrets/ServiceAccount/PVCs each pod actually
    references."""
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/map", {"namespace": namespace})


async def get_cluster_workload_map(cluster_id: str) -> dict:
    """Cluster-wide node -> pod placement map with per-node CPU/memory usage."""
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/clustermap")


async def get_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 100) -> dict:
    """Recent Kubernetes events, newest first. Omit namespace for cluster-wide."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events", {"namespace": namespace, "limit": limit}
    )


async def get_extended_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 200) -> dict:
    """Live events merged with a longer-lived in-memory history, plus
    warning/normal counts."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events/extended", {"namespace": namespace, "limit": limit}
    )


async def get_helm_releases(cluster_id: str) -> dict:
    """List installed Helm releases on the cluster."""
    return await get_client().get(f"/api/clusters/{cluster_id}/helm")


async def get_cost_estimate(cluster_id: str) -> dict:
    """Estimated monthly compute cost per node pool from each cloud's public
    on-demand list pricing."""
    return await get_client().get(f"/api/clusters/{cluster_id}/cost")


async def get_ecosystem_overview(cluster_id: str) -> dict:
    """Which well-known platform CRDs are installed on this cluster."""
    return await get_client().get(f"/api/clusters/{cluster_id}/ecosystem")


async def get_audit_logs(cluster_id: Optional[str] = None, limit: int = 50) -> dict:
    """KubeOrbit's own audit trail of privileged actions taken through the portal."""
    return await get_client().get("/api/audit/logs", {"cluster_id": cluster_id, "limit": limit})


async def diagnose_cluster_issues(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Run KubeOrbit's root-cause-analysis scan."""
    return await get_client().get(
        f"/api/clusters/{cluster_id}/diagnostics/rca", {"namespace": namespace}
    )
