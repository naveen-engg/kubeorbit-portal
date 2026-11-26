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
   and Google ADK inspect parameter annotations at runtime.
2. Keep parameter types simple (`str`, `Optional[str]`, `int`, `bool`) and
   return types as `dict` here so Google ADK's automatic function-calling
   builder works natively; rich MCP `inputSchema` parameter descriptions,
   constraints, titles, and `outputSchema` models live in `mcp_server/schemas.py`
   and are attached when registering tools on FastMCP in `mcp_server/server.py`.
"""
from typing import Optional

from .client import get_client


async def list_teams_and_clusters() -> dict:
    """List all teams, environments, and registered Kubernetes clusters in KubeOrbit's registry without probing live clusters.

    Returns the team hierarchy with each cluster's `cluster_id`, display name, cloud provider (`gke`, `eks`, `aks`, `local`), and environment, with zero credentials or secrets exposed.

    Use as the first discovery call to obtain a valid `cluster_id` required by cluster-scoped tools. Prefer `get_fleet_overview` when you need live node, pod, and health counts probed across every cluster, or `get_cluster_info` once you already know a single `cluster_id`.
    """
    return await get_client().get("/api/teams")


async def get_fleet_overview() -> dict:
    """Aggregate live health, node, pod, namespace, CPU, and memory totals across every registered cluster in one call.

    Probes all configured clusters concurrently and returns a fleet-wide `summary` plus per-cluster status (`ONLINE` or `DEGRADED`) and `health_score`; unreachable clusters degrade gracefully with an `error` field rather than failing the whole call.

    Use for multi-cluster executive health checks or spotting which cluster is degraded. Prefer `list_teams_and_clusters` for instant registry-only `cluster_id` discovery without live probes, or `get_cluster_info` for deep metadata on one specific cluster.
    """
    return await get_client().get("/api/fleet/overview")


async def get_cluster_info(cluster_id: str) -> dict:
    """Retrieve single-cluster control-plane metadata, Kubernetes version, node pools, cloud console links, node list, and Helm release summary.

    Returns `badge`, provider `meta` (version, location, node pools, networking, workload identity), `node_count`, `nodes`, and a lightweight `helm` summary for `cluster_id` (obtained from `list_teams_and_clusters`).

    Use as the starting point for inspecting one cluster's platform configuration. Prefer `get_fleet_overview` to compare all clusters at once, `get_nodes` for a lightweight node-only list, or `get_helm_releases` for Helm releases enriched with live resource health.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/info")


async def get_nodes(cluster_id: str) -> dict:
    """List all Kubernetes nodes in a cluster with readiness status, availability zone, node pool, and allocatable CPU/memory capacity.

    Returns `cluster_id`, `count`, and `items` (one normalized entry per node) for `cluster_id` (from `list_teams_and_clusters`). Strictly read-only with cached cluster lookups.

    Use when checking node readiness, node pool composition, or raw node capacity. Prefer `get_cluster_workload_map` when you also need pod-to-node placement and live CPU/memory utilization, or `get_cost_estimate` for monthly node-pool pricing estimates.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/nodes")


async def get_namespaces(cluster_id: str, include_system: bool = False) -> dict:
    """List namespaces in a cluster along with their status and age, excluding system namespaces by default.

    Returns `cluster_id`, `count`, and `items` for `cluster_id`. By default (`include_system=False`) hides configured system namespaces such as `kube-system`; set `include_system=True` to include control-plane and system namespaces.

    Use to discover valid `namespace` names before calling namespace-scoped tools. Prefer `get_namespace_resources` once you already know the target namespace and want its pods, services, and configs.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces", {"include_system": include_system}
    )


async def get_workloads_overview(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Summarize workload health counts and top unhealthy items across pods, deployments, statefulsets, daemonsets, jobs, and cronjobs.

    Returns `cluster_id`, `namespace`, and `overview` containing `total`, `healthy`, `unhealthy`, and up to 20 unhealthy `issues` per workload kind. Omit `namespace` for a cluster-wide summary (excluding system namespaces by default) or pass a namespace to scope tightly.

    Use as a fast first-pass health triage before drilling deeper. Prefer `get_workloads_catalog` when you need the complete list of every workload and pod, `get_applications` for app-grouped views, or `diagnose_cluster_issues` for 12-Step root-cause analysis and remediation commands.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads", {"namespace": namespace}
    )


async def get_namespace_resources(cluster_id: str, namespace: str) -> dict:
    """Fetch a flat tabular inventory of all core resources in one namespace in a single call.

    Returns `cluster_id`, `namespace`, and separate lists for `pods`, `services`, `configmaps`, `secrets` (metadata only: name, type, and key names — secret values are never decoded), `gateways`, `httproutes`, `pvcs`, and `serviceaccounts`. Large namespaces may return sizable payloads.

    Use when you need a complete inventory of objects in one namespace. Prefer `get_namespace_map` to see dependency edges between those resources, `get_network_topology` or `get_storage_topology` for focused traffic or storage graphs, or `get_pod_details` for deep diagnostics on a single pod.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/namespaces/{namespace}/resources")


async def get_workloads_catalog(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Retrieve the complete itemized catalog of deployments, statefulsets, daemonsets, jobs, cronjobs, and pods with per-workload health flags.

    Returns `cluster_id`, `namespace`, a `summary` rollup, and full arrays (`deployments`, `statefulsets`, `daemonsets`, `jobs`, `cronjobs`, `pods`). Pass `namespace` to filter to one namespace, or omit `namespace` (`null`) to fetch workloads across all namespaces.

    Use when you need to inspect every individual workload object, replica counts, or images. Prefer `get_workloads_overview` for lightweight health counts without full object lists, or `get_applications` when you want workloads and services correlated into logical applications.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/catalog", {"namespace": namespace}
    )


async def get_rollout_history(cluster_id: str, namespace: str, workload_type: str, name: str) -> dict:
    """Inspect native Kubernetes controller revision history for a single Deployment, StatefulSet, or DaemonSet.

    Returns `cluster_id`, `workload_type`, `name`, `namespace`, `current_revision`, and `revisions` (ordered newest-first with revision number, `change_cause`, and container `image`). Strictly read-only and never performs rollbacks.

    Use when tracing image changes or rollout regressions on a Kubernetes workload controller. Prefer `get_helm_release_history` and `diff_helm_revisions` when inspecting Helm chart release revisions, or `get_manifest` to view the workload's full current YAML specification.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/workloads/{workload_type}/{name}/rollout/history",
        {"namespace": namespace},
    )


async def get_applications(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Group Deployments, StatefulSets, DaemonSets, Services, and Pods into logical applications with aggregated replica health.

    Correlates resources using standard application labels (such as `app.kubernetes.io/name`, `app`) or the resource name's leading hyphen prefix, returning `cluster_id`, `namespace`, `count`, `healthy_count`, and grouped `items`. Omit `namespace` (`null`) to scan across namespaces, or pass a namespace to scope.

    Use for an application-centric view of services and their backing workloads. Prefer `get_workloads_overview` for raw counts by Kubernetes kind, `get_workloads_catalog` for ungrouped workload lists, or `get_namespace_map` for explicit pod-to-config/secret/PVC dependency edges.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/applications", {"namespace": namespace}
    )


async def get_pod_details(cluster_id: str, namespace: str, pod_name: str) -> dict:
    """Inspect a single pod in depth, including container states, probe configs, restart reasons, 12-Step pipeline status, matching services, and a 150-line log tail.

    Returns `cluster_id`, pod metadata (`name`, `namespace`, `phase`, `node_name`, `pod_ip`, `labels`), `containers` (probes, requests/limits, termination/waiting reasons), `conditions`, `pipeline_steps`, `matching_services`, and `recent_logs` for the primary container.

    Use as the primary diagnostic call for one failing, crashing, or unready pod. Prefer `get_pod_logs` to fetch longer log tails (`up to 5000 lines`) or logs from a non-primary/sidecar container, or `get_manifest` for the exact raw pod YAML.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/details"
    )


async def get_pod_logs(
    cluster_id: str, namespace: str, pod_name: str, container: Optional[str] = None, tail_lines: int = 200
) -> dict:
    """Fetch the most recent stdout/stderr log lines from a pod or one specific container inside it.

    Returns `cluster_id`, `namespace`, `pod_name`, `container`, `tail_lines`, and raw `logs` text. Omit `container` for single-container pods (or default container) and adjust `tail_lines` between `10` and `5000` (default `200`).

    Use when reading application stack traces, startup logs, or sidecar container output. Prefer `get_pod_details` when you also need probe definitions, OOM/exit codes, and 12-Step lifecycle status, or `get_events` for Kubernetes scheduling and kubelet events.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/namespaces/{namespace}/pods/{pod_name}/logs",
        {"container": container, "tail_lines": tail_lines},
    )


async def get_manifest(cluster_id: str, kind: str, name: str, namespace: Optional[str] = None) -> dict:
    """Retrieve the read-only raw YAML manifest (`kubectl get -o yaml`) for one non-Secret Kubernetes resource.

    Returns `cluster_id`, `namespace`, `kind`, `name`, and `yaml`. Pass `namespace` for namespaced resources (Pod, Deployment, Service, ConfigMap, PVC) or omit it for cluster-scoped resources (Node, StorageClass, PV, Namespace). Requests for `Secret` resources are hard-blocked with HTTP `403 Forbidden`.

    Use when you need exact raw spec fields, annotations, or env/volume mounts not present in summarized views. Prefer `get_pod_details` for structured pod diagnostics, `get_namespace_resources` to list Secret metadata safely, or `get_helm_release_details` for masked Helm-rendered manifests.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/manifest", {"kind": kind, "name": name, "namespace": namespace}
    )


async def get_network_topology(cluster_id: str, namespace: str) -> dict:
    """Build the inbound network traffic graph (`ILB/Ingress -> Gateway -> HTTPRoute -> Service -> Pod`) for one namespace.

    Returns `cluster_id`, `namespace`, `gateway_api_available`, `nodes` (with `id`, `label`, `kind`, `status`, `meta`), and directed `edges` (`source` -> `target`) tracing traffic paths into pods.

    Use when debugging ingress routing, Gateway API HTTPRoutes, or Service-to-Pod selector wiring. Prefer `get_storage_topology` for PV/PVC volume wiring, `get_namespace_map` for all resource dependencies together, or `get_namespace_resources` for flat tabular lists.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/network", {"namespace": namespace}
    )


async def get_storage_topology(cluster_id: str, namespace: str) -> dict:
    """Build the persistent storage binding graph (`StorageClass -> PersistentVolume -> PersistentVolumeClaim -> Pod`) for one namespace.

    Returns `cluster_id`, `namespace`, `nodes` (with `id`, `label`, `kind`, `status`, `meta`), and directed `edges` (`source` -> `target`) showing how StorageClasses provision PVs bound to PVCs mounted by Pods.

    Use when diagnosing `Pending` PVCs, missing StorageClasses, or volume mount bindings. Prefer `get_network_topology` for traffic routing, `get_namespace_map` for full namespace dependencies (including ConfigMaps and Secrets), or `get_namespace_resources` for flat object tables.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/topology/storage", {"namespace": namespace}
    )


async def get_namespace_map(cluster_id: str, namespace: str) -> dict:
    """Build the end-to-end dependency graph of all workloads, routing, configs, secret metadata, ServiceAccounts, and PVCs in one namespace.

    Returns `cluster_id`, `namespace`, `ingress_available`, `gateway_api_available`, `nodes` (tagged with `app_group`, `kind`, `status`, `meta`; Secret nodes expose metadata only), and directed `edges` connecting Ingress/Gateway/Services -> Pods, Workloads -> Pods, and ConfigMaps/Secrets/PVCs/ServiceAccounts -> Pods.

    Use to understand how every resource in a namespace is wired together and which configs or volumes a pod depends on. Prefer `get_network_topology` or `get_storage_topology` for narrower single-path graphs, `get_namespace_resources` for flat lists, or `get_cluster_workload_map` for cluster-wide node-to-pod placement.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/map", {"namespace": namespace})


async def get_cluster_workload_map(cluster_id: str) -> dict:
    """Map pod placement across every cluster node with per-node CPU, memory, pod capacity, and namespace breakdown.

    Returns `cluster_id`, `summary` (node, ready, scheduled pod, and namespace counts), `namespaces` legend, and per-node `nodes` cards with live `kubectl top nodes` utilization (or conservative fallback if metrics-server is unavailable). Excludes `kube-system` pods to highlight user workloads.

    Use to spot unbalanced scheduling, hot nodes, or where a namespace's pods are running across nodes. Prefer `get_nodes` for lightweight node inventory without pod placement, or `get_namespace_map` for intra-namespace resource dependencies.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/topology/clustermap")


async def get_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 100) -> dict:
    """Fetch live Kubernetes API events currently retained by the cluster, ordered newest-first.

    Returns `cluster_id`, `namespace`, `count`, and `items` (capped by `limit`, `1-500`, default `100`). Reflects only events still live in the cluster API server (typically ~1 hour before Kubernetes garbage collection). Omit `namespace` for cluster-wide events.

    Use for quick checks of current cluster or namespace warnings. Prefer `get_extended_events` when investigating older incidents that may have aged out of Kubernetes' 1-hour event TTL or when you need `warning_count`/`normal_count` statistics, or `get_audit_logs` for KubeOrbit portal audit entries.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events", {"namespace": namespace, "limit": limit}
    )


async def get_extended_events(cluster_id: str, namespace: Optional[str] = None, limit: int = 200) -> dict:
    """Retrieve live Kubernetes events merged with KubeOrbit's in-memory historical ring buffer plus warning/normal summary counts.

    Retains up to 1,000 recent unique events per cluster in server memory so transient failures remain visible after Kubernetes' ~1-hour API event eviction. Returns `cluster_id`, `namespace`, `items` (capped by `limit`, `10-1000`, default `200`), `total_events`, `warning_count`, and `normal_count`. Omit `namespace` for cluster-wide history.

    Use for post-incident timelines, flapping workloads, or when you need warning vs normal event counts. Prefer `get_events` for strictly live API server events with lower payload size, or `get_audit_logs` for portal-level user actions.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/events/extended", {"namespace": namespace, "limit": limit}
    )


async def get_helm_releases(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """List installed Helm releases on a cluster with current revision, chart/app versions, status, and live rendered resource health.

    Returns `cluster_id`, `available` (whether `helm` CLI is present), `namespaces`, and `items` enriched with each release's rendered Kubernetes `resources` and `resource_count`. Pass `namespace` to filter to one namespace, or omit `namespace` (`null`) to list releases cluster-wide.

    Use to discover Helm release names, namespaces, and overall deployment health. Prefer `get_helm_release_details` to inspect values and manifests for one release, `get_helm_release_history` for its revision timeline, or `diff_helm_revisions` to compare two revisions.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/helm", {"namespace": namespace})


async def get_helm_release_details(
    cluster_id: str,
    namespace: str,
    release_name: str,
    revision: Optional[int] = None,
) -> dict:
    """Inspect one Helm release revision's metadata, masked user/computed values, masked manifest YAML, notes, and live resource status.

    Returns `cluster_id`, release metadata (`name`, `namespace`, `revision`, `current_revision`, `chart`, `app_version`, `status`), `user_values_yaml`, `computed_values_yaml`, `manifest_yaml`, `notes`, and `resources`. All Kubernetes Secret data and credential-like keys are masked server-side with deterministic SHA-256 fingerprints (`masked_server_side=True`). Omit `revision` (`null`) for the latest revision, or pass a positive integer (`>=1`); returns `404` if the release does not exist.

    Use to examine a single release revision's configuration and rendered resources safely. Prefer `get_helm_releases` to list installed releases, `get_helm_release_history` to discover revision numbers, or `diff_helm_revisions` to compare changes between two revisions.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/helm/{namespace}/{release_name}",
        {"revision": revision},
    )


async def get_helm_release_history(
    cluster_id: str,
    namespace: str,
    release_name: str,
    max_revisions: int = 256,
) -> dict:
    """List the chronological revision history for one Helm release ordered newest-first.

    Returns `cluster_id`, `release`, `namespace`, `current_revision`, `earliest_revision`, `count`, and `items` (each containing `revision`, `status`, `chart`, `app_version`, `updated`, `description`, and `is_current`), capped by `max_revisions` (`1-256`, default `256`). Returns `404` if the release is not found.

    Use to audit upgrade/rollback history and choose `from_revision`/`to_revision` numbers before calling `diff_helm_revisions`. Prefer `get_helm_release_details` to read a specific revision's values/manifests, or `get_rollout_history` for non-Helm native Deployment/StatefulSet/DaemonSet revisions.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/helm/{namespace}/{release_name}/history",
        {"max_revisions": max_revisions},
    )


async def diff_helm_revisions(
    cluster_id: str,
    namespace: str,
    release_name: str,
    from_revision: int,
    to_revision: int,
    mode: str = "values",
) -> dict:
    """Compare two revisions of a Helm release across user values, computed values, or rendered manifests with server-side secret masking.

    Returns `cluster_id`, `release`, `namespace`, `from_revision`, `to_revision`, `compare_mode`, revision metadata (`from_meta`, `to_meta`), `masked_server_side=True`, and structured diff hunks/lines/stats. Secret values are replaced server-side with SHA-256 change fingerprints so rotations are detectable without leaking plaintext. Set `mode` to `"values"` (user overrides, default), `"all_values"` (merged chart defaults + overrides), or `"manifest"` (rendered Kubernetes YAML).

    Use to pinpoint configuration or manifest drift between two Helm revisions (get revision numbers from `get_helm_release_history`). Prefer `get_helm_release_details` to view a single revision in full, or `get_rollout_history` for native Kubernetes workload revisions.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/helm/{namespace}/{release_name}/diff",
        {"from_revision": from_revision, "to_revision": to_revision, "mode": mode},
    )


async def get_cost_estimate(cluster_id: str) -> dict:
    """Estimate monthly compute spend per node pool for one cluster using public cloud on-demand list pricing.

    Returns `cluster_id`, `provider`, `currency` (`USD`), `monthly_total_usd`, per-pool `pools` breakdown (`machine_type`, `node_count`, `hourly_price_usd`, `monthly_estimate_usd`, `priced`, `note`), and a mandatory `disclaimer`. Compute-only public list estimate: excludes committed-use/reserved/spot discounts, storage, egress, load balancers, and control-plane fees; unsupported machine families are marked `priced=False`.

    Use for ballpark compute cost sizing and node-pool right-sizing checks. Prefer `get_nodes` or `get_cluster_info` for node hardware capacity and autoscaling bounds without pricing lookups.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/cost")


async def get_ecosystem_overview(cluster_id: str) -> dict:
    """Detect which well-known Kubernetes platform CRD ecosystems are installed on a cluster and count their custom resources.

    Checks 12 common platform ecosystems (`cert-manager`, `KEDA`, `Karpenter`, `Velero`, `External Secrets`, `Kyverno`, `Sealed Secrets`, `CloudNativePG`, `Knative`, `Trivy`, `Argo CD`, and `Flux`) using cached CRD discovery (`300s` TTL). Returns `cluster_id`, `detected_count`, `total_checked`, and `integrations` grouped by category with `installed` flags and per-CRD `count` (presence and counts only, not individual CR specs).

    Use to quickly see what platform add-ons and GitOps/policy controllers exist on a cluster. Prefer `get_manifest` to inspect a specific custom resource's YAML once detected, or `get_cluster_info` for core Kubernetes version and cloud provider metadata.
    """
    return await get_client().get(f"/api/clusters/{cluster_id}/ecosystem")


async def get_audit_logs(cluster_id: Optional[str] = None, limit: int = 50) -> dict:
    """Fetch KubeOrbit's portal-level audit log of privileged administrative or operator actions.

    Returns `count` and `items` (timestamp, user, action such as `ONBOARD_CLUSTER`/`UPDATE_CLUSTER`/`DELETE_CLUSTER`/`ROLLBACK_WORKLOAD`, `cluster_id`, `namespace`, `resource`, and `details`), capped by `limit` (`1-500`, default `50`). Pass `cluster_id` to filter to one cluster, or omit `cluster_id` (`null`) for portal-wide audit entries.

    Use to review who performed privileged changes through the KubeOrbit portal. Prefer `get_events` or `get_extended_events` for Kubernetes cluster runtime events, and `get_pod_logs` for container application logs.
    """
    return await get_client().get("/api/audit/logs", {"cluster_id": cluster_id, "limit": limit})


async def diagnose_cluster_issues(cluster_id: str, namespace: Optional[str] = None) -> dict:
    """Run automated 12-Step `kubectl apply` Root-Cause Analysis (RCA) across a cluster or single namespace to detect stalled workloads and reliability gaps.

    Inspects pods, events, nodes, deployments, statefulsets, daemonsets, services, configmaps, network policies, and Helm releases, folding symptoms across Steps 1-12 (API authn/authz, controllers, kube-scheduler, kubelet mount/CNI/image-pull/runtime, readiness probes, and EndpointSlice routing) into ranked `root_causes` with severity, confidence, and copy-paste `remediation_cmd` values. Returns `cluster_id`, `namespace`, `health_score`, `summary`, `step_counts`, `pipeline_stages`, `components`, `integrations` (Helm/Argo CD/Prometheus), `root_causes`, and `raw_issues`. Omit `namespace` (`null`) to scan all non-system namespaces, or pass one namespace to scope.

    Use as the primary automated troubleshooting tool when investigating unhealthy workloads, CrashLoopBackOff, ImagePullBackOff, Pending pods, or unready services. Prefer `get_workloads_overview` for raw healthy/unhealthy counts without RCA folding, or `get_pod_details` and `get_pod_logs` to drill into a specific flagged pod.
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/diagnostics/rca", {"namespace": namespace}
    )


async def simulate_network_policy(
    cluster_id: str,
    source_namespace: str,
    source_pod: str,
    dest_namespace: str,
    dest_pod: Optional[str] = None,
    dest_service: Optional[str] = None,
    port: Optional[int] = None,
    protocol: str = "TCP",
) -> dict:
    """Simulate static L3/L4 Kubernetes NetworkPolicy reachability between a source pod and a destination pod or Service without sending live traffic.

    Evaluates all matching Egress NetworkPolicies in `source_namespace` and Ingress NetworkPolicies in `dest_namespace` (including `podSelector` + `namespaceSelector` AND/OR semantics, `ipBlock` CIDR/except ranges, named container ports, `endPort`, and default-allow vs. default-deny baselines). Returns `cluster_id`, `allowed`, `verdict` (`ALLOWED`, `BLOCKED_EGRESS`, `BLOCKED_INGRESS`, or `BLOCKED_BOTH`), `egress_allowed`, `ingress_allowed`, `selecting_egress_policies`, `selecting_ingress_policies`, `allowing_egress_policies`, `allowing_ingress_policies`, `blocking_reasons`, and `suggested_fix_hint`.

    Use whenever pods or Services time out silently or after `diagnose_cluster_issues` flags CNI NetworkPolicy isolation. Provide at least one of `dest_pod` or `dest_service` in `dest_namespace` (prefer `get_network_topology` or `get_namespace_resources` to discover pod/Service names).
    """
    return await get_client().get(
        f"/api/clusters/{cluster_id}/networkpolicy/simulate",
        {
            "source_namespace": source_namespace,
            "source_pod": source_pod,
            "dest_namespace": dest_namespace,
            "dest_pod": dest_pod,
            "dest_service": dest_service,
            "port": port,
            "protocol": protocol,
        },
    )
