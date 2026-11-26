"""MCP JSON Schema metadata (titles, parameter descriptions/constraints, and
structured Pydantic output schemas) for KubeOrbit's 27 read-only MCP tools.

Kept separate from `mcp_server/tools.py` so the raw async tool functions stay
directly compatible with Google ADK's automatic function-calling parser (which
expects simple Python annotations), while `mcp_server/server.py` attaches full
`inputSchema` parameter descriptions/constraints and `outputSchema` models when
registering each tool on FastMCP.

NOTE: Do not add `from __future__ import annotations` here — FastMCP inspects
wrapped parameter and return annotations at runtime.
"""
import inspect
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# --------------------------------------------------------------------------- #
# Reusable constrained string types matching app.py route validation
# --------------------------------------------------------------------------- #

_K8S_CLUSTER_ID_REGEX = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$"
_K8S_NAME_REGEX = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}$"
_K8S_NS_REGEX = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$"
_K8S_KIND_REGEX = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"

_ClusterIdStr = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=_K8S_CLUSTER_ID_REGEX)
]
_NamespaceStr = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=_K8S_NS_REGEX)
]
_ResourceNameStr = Annotated[
    str, StringConstraints(min_length=1, max_length=253, pattern=_K8S_NAME_REGEX)
]
_ContainerNameStr = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=_K8S_NS_REGEX)
]
_KindStr = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=_K8S_KIND_REGEX)
]

# --------------------------------------------------------------------------- #
# Parameter annotations (100% JSON Schema description & constraint coverage)
# --------------------------------------------------------------------------- #

ClusterIdParam = Annotated[
    _ClusterIdStr,
    Field(
        description=(
            "Unique KubeOrbit cluster identifier returned by `list_teams_and_clusters` "
            "(1-63 URL-safe chars, e.g. 'gke-prod-us', 'eks-staging')."
        ),
    ),
]

OptionalClusterIdParam = Annotated[
    Optional[_ClusterIdStr],
    Field(
        description=(
            "Optional cluster identifier (from `list_teams_and_clusters`) to filter audit entries "
            "to one cluster. Omit or pass null to return portal-wide audit logs across all clusters."
        ),
    ),
]

RequiredNamespaceParam = Annotated[
    _NamespaceStr,
    Field(
        description=(
            "Target Kubernetes namespace name (1-63 chars, e.g. 'default', 'prod'). "
            "Use `get_namespaces` to discover available namespaces on the cluster."
        ),
    ),
]

OptionalNamespaceAllParam = Annotated[
    Optional[_NamespaceStr],
    Field(
        description=(
            "Optional Kubernetes namespace name (1-63 chars) to scope results to a single namespace. "
            "Omit or pass null to query across all namespaces in the cluster."
        ),
    ),
]

OptionalNamespaceNonSystemParam = Annotated[
    Optional[_NamespaceStr],
    Field(
        description=(
            "Optional Kubernetes namespace name (1-63 chars) to scope results to a single namespace. "
            "Omit or pass null to query across all non-system namespaces (system namespaces such as "
            "kube-system are excluded by default)."
        ),
    ),
]

ManifestNamespaceParam = Annotated[
    Optional[_NamespaceStr],
    Field(
        description=(
            "Kubernetes namespace name (1-63 chars) for namespaced resources (Pod, Deployment, "
            "Service, ConfigMap, PVC). Omit or pass null for cluster-scoped resources "
            "(Node, Namespace, PersistentVolume, StorageClass)."
        ),
    ),
]

IncludeSystemParam = Annotated[
    bool,
    Field(
        description=(
            "When false (default), excludes configured system namespaces (e.g. kube-system, "
            "kube-public, kube-node-lease) to match the portal's default view. Set to true "
            "to include all system and control-plane namespaces."
        ),
    ),
]

WorkloadTypeParam = Annotated[
    Literal["deployment", "statefulset", "daemonset"],
    Field(
        description=(
            "Kubernetes controller kind supporting rollout history: one of "
            "'deployment', 'statefulset', or 'daemonset'."
        ),
    ),
]

WorkloadNameParam = Annotated[
    _ResourceNameStr,
    Field(
        description=(
            "Exact name of the Deployment, StatefulSet, or DaemonSet inside `namespace` "
            "(1-253 chars, e.g. 'checkout-api')."
        ),
    ),
]

PodNameParam = Annotated[
    _ResourceNameStr,
    Field(
        description=(
            "Exact Kubernetes pod name inside `namespace` (1-253 chars, e.g. 'api-7c9f8b-x9k2p'). "
            "Obtain pod names from `get_namespace_resources` or `get_workloads_catalog`."
        ),
    ),
]

ContainerNameParam = Annotated[
    Optional[_ContainerNameStr],
    Field(
        description=(
            "Optional container name (1-63 chars) inside a multi-container or sidecar pod. "
            "Omit or pass null to fetch logs from the pod's default/primary container."
        ),
    ),
]

TailLinesParam = Annotated[
    int,
    Field(
        ge=10,
        le=5000,
        description="Number of trailing log lines to return from the end of the container log stream (10-5000, default 200).",
    ),
]

ResourceKindParam = Annotated[
    _KindStr,
    Field(
        description=(
            "Kubernetes resource kind or plural/group name (e.g. 'Deployment', 'Pod', 'Service', "
            "'ConfigMap', 'Ingress', 'HTTPRoute', 'Node'). Requests for 'Secret' / 'secrets' "
            "are permanently blocked with HTTP 403."
        ),
    ),
]

ResourceNameParam = Annotated[
    _ResourceNameStr,
    Field(
        description="Exact Kubernetes metadata.name of the resource to inspect (1-253 chars).",
    ),
]

EventsLimitParam = Annotated[
    int,
    Field(
        ge=1,
        le=500,
        description="Maximum number of live Kubernetes events to return, ordered newest-first (1-500, default 100).",
    ),
]

ExtendedEventsLimitParam = Annotated[
    int,
    Field(
        ge=10,
        le=1000,
        description=(
            "Maximum number of merged live + historical ring-buffer events to return, "
            "ordered newest-first (10-1000, default 200)."
        ),
    ),
]

HelmReleaseNameParam = Annotated[
    _ResourceNameStr,
    Field(
        description=(
            "Exact Helm release name inside `namespace` (1-253 chars, e.g. 'ingress-nginx'). "
            "Discover installed releases via `get_helm_releases`."
        ),
    ),
]

OptionalRevisionParam = Annotated[
    Optional[Annotated[int, Field(ge=1)]],
    Field(
        description=(
            "Optional positive Helm revision number (>=1) to inspect. "
            "Omit or pass null to inspect the release's latest/current revision."
        ),
    ),
]

MaxRevisionsParam = Annotated[
    int,
    Field(
        ge=1,
        le=256,
        description="Maximum number of historical Helm revisions to return, ordered newest-first (1-256, default 256).",
    ),
]

FromRevisionParam = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Base (older) Helm revision number (>=1) to diff from. "
            "Obtain valid revision numbers from `get_helm_release_history`."
        ),
    ),
]

ToRevisionParam = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Target (newer) Helm revision number (>=1) to diff against `from_revision`. "
            "Obtain valid revision numbers from `get_helm_release_history`."
        ),
    ),
]

HelmDiffModeParam = Annotated[
    Literal["values", "all_values", "manifest"],
    Field(
        description=(
            "Artifact type to diff between revisions: 'values' (user-supplied overrides only, default), "
            "'all_values' (merged chart defaults + overrides), or 'manifest' (rendered Kubernetes YAML). "
            "All secrets are masked server-side in every mode."
        ),
    ),
]

AuditLimitParam = Annotated[
    int,
    Field(
        ge=1,
        le=500,
        description="Maximum number of portal audit log entries to return, ordered newest-first (1-500, default 50).",
    ),
]

SourceNamespaceParam = Annotated[
    _NamespaceStr,
    Field(description="Kubernetes namespace of the source pod initiating traffic (1-63 chars)."),
]

SourcePodParam = Annotated[
    _ResourceNameStr,
    Field(description="Exact pod name of the traffic source inside `source_namespace` (1-253 chars)."),
]

DestNamespaceParam = Annotated[
    _NamespaceStr,
    Field(description="Kubernetes namespace of the destination pod or Service (1-63 chars)."),
]

DestPodParam = Annotated[
    Optional[_ResourceNameStr],
    Field(
        description=(
            "Optional exact destination pod name inside `dest_namespace`. "
            "Provide either `dest_pod` or `dest_service`."
        ),
    ),
]

DestServiceParam = Annotated[
    Optional[_ResourceNameStr],
    Field(
        description=(
            "Optional Kubernetes Service name inside `dest_namespace` (resolved to a backend pod via its label selector). "
            "Provide either `dest_pod` or `dest_service`."
        ),
    ),
]

NetPolPortParam = Annotated[
    Optional[Annotated[int, Field(ge=1, le=65535)]],
    Field(
        description=(
            "Optional L4 destination port number (1-65535). When omitted with `dest_service`, inferred from the Service's first targetPort."
        ),
    ),
]

NetPolProtocolParam = Annotated[
    Literal["TCP", "UDP", "SCTP"],
    Field(description="Network protocol to evaluate against NetworkPolicy port rules: 'TCP' (default), 'UDP', or 'SCTP'."),
]


# --------------------------------------------------------------------------- #
# Structured output models (emitted as MCP `outputSchema` in tools/list)
# All models set `extra="allow"` and safe defaults so runtime API responses
# always validate cleanly even when optional/provider-specific keys vary.
# --------------------------------------------------------------------------- #


class _FlexibleOutput(BaseModel):
    model_config = ConfigDict(extra="allow")


class TeamsAndClustersOutput(_FlexibleOutput):
    teams: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Registered teams, each grouping environments and clusters with cluster_id, name, provider, and description.",
    )


class FleetOverviewOutput(_FlexibleOutput):
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Aggregated fleet totals: total_clusters, healthy_clusters, online_clusters, "
            "degraded_clusters, fleet_health_score, total_nodes, total_pods, total_namespaces, "
            "total_cpu_cores, and total_memory_gib."
        ),
    )
    clusters: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-cluster live probe cards with id, name, provider, environment, status (ONLINE/DEGRADED), counts, and health_score.",
    )


class ClusterInfoOutput(_FlexibleOutput):
    badge: Dict[str, Any] = Field(
        default_factory=dict,
        description="Cluster display metadata: cluster_id, name, team_id, team_name, environment, and provider badge details.",
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Cloud control-plane metadata: kubernetes_version, location, status, node_pools, console_url, logs_url, workload_identity, and network.",
    )
    node_count: int = Field(default=0, description="Total number of nodes detected in the cluster.")
    node_fetch_error: Optional[str] = Field(
        default=None, description="Error message if live node listing failed while provider metadata succeeded."
    )
    helm: Dict[str, Any] = Field(
        default_factory=dict,
        description="Lightweight Helm release summary (`available`, `items`, `namespaces`) without per-release resource enrichment.",
    )
    nodes: List[Dict[str, Any]] = Field(
        default_factory=list, description="Normalized node summary entries for the cluster."
    )


class NodesOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Normalized node objects containing name, status/ready, roles, zone, node_pool, cpu_capacity, memory_capacity, and age.",
    )
    count: int = Field(default=0, description="Total number of nodes returned.")


class NamespacesOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Normalized namespace entries with name, status, labels, and age.",
    )
    count: int = Field(default=0, description="Total number of namespaces returned.")


class WorkloadsOverviewOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(
        default=None, description="Scoped namespace name, or null when aggregated across non-system namespaces."
    )
    overview: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-kind health rollups for pods, deployments, statefulsets, daemonsets, jobs "
            "(each with total, healthy, unhealthy, and up to 20 unhealthy issues) plus cronjobs (total, active)."
        ),
    )


class NamespaceResourcesOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: str = Field(default="", description="Namespace inspected.")
    pods: List[Dict[str, Any]] = Field(default_factory=list, description="Pods in the namespace with phase, readiness, restarts, node, and age.")
    services: List[Dict[str, Any]] = Field(default_factory=list, description="Services with type, cluster IP, ports, external IPs, and selectors.")
    configmaps: List[Dict[str, Any]] = Field(default_factory=list, description="ConfigMaps with name, key names, and metadata.")
    secrets: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Secret metadata only (name, type, key names, age). Plaintext secret values are never returned.",
    )
    gateways: Dict[str, Any] = Field(
        default_factory=dict, description="Gateway API Gateways (`available` flag plus `items`)."
    )
    httproutes: Dict[str, Any] = Field(
        default_factory=dict, description="Gateway API HTTPRoutes (`available` flag plus `items`)."
    )
    pvcs: List[Dict[str, Any]] = Field(default_factory=list, description="PersistentVolumeClaims with status, capacity, storage class, and bound volume.")
    serviceaccounts: List[Dict[str, Any]] = Field(default_factory=list, description="ServiceAccounts configured in the namespace.")


class WorkloadsCatalogOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(
        default=None, description="Filtered namespace name, or null when listing across all namespaces."
    )
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Aggregate counts: total_workloads, healthy_count, unhealthy_count, and per-kind counts.",
    )
    deployments: List[Dict[str, Any]] = Field(default_factory=list, description="Deployments with replica counts, images, and `healthy` flag.")
    statefulsets: List[Dict[str, Any]] = Field(default_factory=list, description="StatefulSets with replica counts and `healthy` flag.")
    daemonsets: List[Dict[str, Any]] = Field(default_factory=list, description="DaemonSets with scheduled/ready node counts and `healthy` flag.")
    jobs: List[Dict[str, Any]] = Field(default_factory=list, description="Batch Jobs with completion/failure status.")
    cronjobs: List[Dict[str, Any]] = Field(default_factory=list, description="CronJobs with schedule, suspend state, and active job counts.")
    pods: List[Dict[str, Any]] = Field(default_factory=list, description="Pods with phase, readiness, restarts, node placement, and metrics.")


class RolloutHistoryOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    workload_type: str = Field(default="", description="Workload controller kind queried (deployment, statefulset, or daemonset).")
    name: str = Field(default="", description="Workload controller name.")
    namespace: str = Field(default="", description="Namespace containing the workload.")
    current_revision: Optional[int] = Field(default=None, description="Latest revision number observed.")
    revisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Revision entries ordered newest-first, each with `revision`, `change_cause`, and container `image` when available.",
    )


class ApplicationsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(
        default=None, description="Filtered namespace name, or null when grouped across namespaces."
    )
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Logical application groups correlated by app labels or name prefix, each with name, namespace, "
            "healthy, total_replicas, ready_replicas, deployments, statefulsets, daemonsets, services, and pods."
        ),
    )
    count: int = Field(default=0, description="Total number of logical applications discovered.")
    healthy_count: int = Field(default=0, description="Number of logical applications currently healthy.")


class PodDetailsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    name: str = Field(default="", description="Pod name.")
    namespace: str = Field(default="", description="Pod namespace.")
    phase: str = Field(default="", description="Kubernetes pod phase (Running, Pending, Failed, Succeeded, Unknown).")
    node_name: Optional[str] = Field(default=None, description="Node currently running the pod, or null if unscheduled.")
    pod_ip: Optional[str] = Field(default=None, description="Pod IP address assigned by CNI.")
    host_ip: Optional[str] = Field(default=None, description="Host node IP address.")
    start_time: Optional[str] = Field(default=None, description="ISO timestamp when kubelet acknowledged the pod.")
    containers: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-container state, image, readiness, restarts, last termination/waiting reason, requests/limits, and liveness/readiness/startup probes.",
    )
    conditions: List[Dict[str, Any]] = Field(default_factory=list, description="PodScheduled, Initialized, ContainersReady, and Ready conditions.")
    pipeline_steps: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="12-Step `kubectl apply` lifecycle checklist (Steps 1-8, 9, 10a/b, 10c/d, 11, 12) with passed/failed/pending status.",
    )
    matching_services: List[Dict[str, Any]] = Field(
        default_factory=list, description="Services in the namespace whose label selector matches this pod."
    )
    recent_logs: str = Field(default="", description="Recent trailing logs (up to 150 lines) from the primary container.")
    labels: Dict[str, str] = Field(default_factory=dict, description="Kubernetes metadata labels attached to the pod.")


class PodLogsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: str = Field(default="", description="Pod namespace.")
    pod_name: str = Field(default="", description="Pod name.")
    container: Optional[str] = Field(default=None, description="Container queried, or null when using the default container.")
    tail_lines: int = Field(default=200, description="Maximum trailing line count requested.")
    logs: str = Field(default="", description="Raw newline-delimited container stdout/stderr log output.")


class ManifestOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(default=None, description="Resource namespace, or null for cluster-scoped objects.")
    kind: str = Field(default="", description="Kubernetes resource kind queried.")
    name: str = Field(default="", description="Kubernetes resource name.")
    yaml: str = Field(default="", description="Read-only YAML manifest returned by `kubectl get -o yaml`.")


class NetworkTopologyOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: str = Field(default="", description="Namespace inspected.")
    gateway_api_available: bool = Field(
        default=False, description="True when Gateway API CRDs (Gateway and HTTPRoute) are installed on the cluster."
    )
    nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Graph vertices (`id`, `label`, `kind` in ilb/ingress/gateway/httproute/service/pod, `status`, `meta`).",
    )
    edges: List[Dict[str, str]] = Field(
        default_factory=list, description="Directed routing edges (`source` -> `target`)."
    )


class StorageTopologyOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: str = Field(default="", description="Namespace inspected.")
    nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Graph vertices (`id`, `label`, `kind` in storageclass/pv/pvc/pod, `status`, `meta`).",
    )
    edges: List[Dict[str, str]] = Field(
        default_factory=list, description="Directed storage-binding edges (`source` -> `target`)."
    )


class NamespaceMapOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: str = Field(default="", description="Namespace inspected.")
    ingress_available: bool = Field(default=False, description="True when Ingress listing is available.")
    gateway_api_available: bool = Field(default=False, description="True when Gateway API CRDs are installed.")
    nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Full namespace resource graph nodes (`id`, `label`, `kind`, `group`, `app_group`, `status`, `meta`). "
            "Secret nodes expose metadata only."
        ),
    )
    edges: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Directed dependency edges connecting traffic, workload controllers, pods, ConfigMaps, Secrets, ServiceAccounts, and PVCs.",
    )


class ClusterWorkloadMapOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Cluster placement summary: total nodes, ready nodes, pods_scheduled (excluding kube-system), and namespace count.",
    )
    namespaces: List[Dict[str, Any]] = Field(
        default_factory=list, description="Active non-kube-system namespaces with display color legend."
    )
    nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-node placement cards with status, machine_type, zone, node_pool, cpu/memory utilization, pods_capacity, and per-namespace pod lists.",
    )


class EventsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(default=None, description="Scoped namespace, or null when cluster-wide.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Live Kubernetes events ordered newest-first with type (Normal/Warning), reason, message, involved object, count, and timestamps.",
    )
    count: int = Field(default=0, description="Number of live events returned.")


class ExtendedEventsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    namespace: Optional[str] = Field(default=None, description="Scoped namespace, or null when cluster-wide.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Merged live and in-memory ring-buffer Kubernetes events ordered newest-first.",
    )
    total_events: int = Field(default=0, description="Total matching events retained in the ring buffer before `limit` truncation.")
    warning_count: int = Field(default=0, description="Number of Warning-type events in the matched set.")
    normal_count: int = Field(default=0, description="Number of Normal-type events in the matched set.")


class HelmReleasesOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    available: bool = Field(default=False, description="True when the `helm` CLI is available on the KubeOrbit server.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Installed Helm releases with name, namespace, chart, app_version, revision, status, updated timestamp, and enriched `resources` health.",
    )
    namespaces: List[str] = Field(default_factory=list, description="Distinct namespaces containing at least one Helm release.")
    error: Optional[str] = Field(default=None, description="Error string if `helm list` failed.")


class HelmReleaseDetailsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    name: str = Field(default="", description="Helm release name.")
    namespace: str = Field(default="", description="Helm release namespace.")
    revision: Optional[int] = Field(default=None, description="Helm revision number inspected.")
    current_revision: Optional[int] = Field(default=None, description="Latest revision number deployed for this release.")
    revision_count: int = Field(default=0, description="Total number of revisions in the release history.")
    chart: str = Field(default="", description="Chart name and version (e.g. 'nginx-15.1.0').")
    app_version: str = Field(default="", description="Application version declared by the Helm chart.")
    status: str = Field(default="", description="Helm release status (deployed, superseded, failed, etc.).")
    updated: str = Field(default="", description="Timestamp when this revision was last updated.")
    description: str = Field(default="", description="Helm release status description.")
    masked_server_side: bool = Field(
        default=True, description="Always true: confirms Secret values and credential keys were masked server-side."
    )
    mask_notice: str = Field(default="", description="Security notice describing server-side secret masking.")
    user_values_yaml: str = Field(default="", description="Server-side masked user-supplied Helm values YAML.")
    computed_values_yaml: str = Field(default="", description="Server-side masked merged/computed Helm values YAML.")
    manifest_yaml: str = Field(default="", description="Server-side masked rendered Kubernetes manifest YAML.")
    notes: str = Field(default="", description="Masked Helm release post-install notes.")
    resources: List[Dict[str, Any]] = Field(
        default_factory=list, description="Rendered Kubernetes resources enriched with live cluster health."
    )
    resource_count: int = Field(default=0, description="Count of rendered Kubernetes resources.")


class HelmReleaseHistoryOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    release: str = Field(default="", description="Helm release name.")
    namespace: str = Field(default="", description="Helm release namespace.")
    current_revision: Optional[int] = Field(default=None, description="Newest revision number in the release history.")
    earliest_revision: Optional[int] = Field(default=None, description="Oldest retained revision number.")
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Revision entries ordered newest-first with revision, status, chart, app_version, updated, description, and is_current.",
    )
    count: int = Field(default=0, description="Number of revisions returned.")


class HelmDiffOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    release: str = Field(default="", description="Helm release name.")
    namespace: str = Field(default="", description="Helm release namespace.")
    from_revision: int = Field(default=1, description="Base revision number compared from.")
    to_revision: int = Field(default=1, description="Target revision number compared to.")
    compare_mode: str = Field(default="values", description="Comparison mode executed: values, all_values, or manifest.")
    from_meta: Dict[str, Any] = Field(default_factory=dict, description="History metadata for `from_revision`.")
    to_meta: Dict[str, Any] = Field(default_factory=dict, description="History metadata for `to_revision`.")
    masked_server_side: bool = Field(
        default=True, description="Always true: secrets were replaced server-side with SHA-256 change fingerprints before diffing."
    )
    mask_notice: str = Field(default="", description="Security notice describing server-side secret masking.")
    has_changes: bool = Field(default=False, description="True when at least one line differs between the two masked revisions.")
    stats: Dict[str, Any] = Field(default_factory=dict, description="Line-level diff counts (added, removed, changed).")
    hunks: List[Dict[str, Any]] = Field(default_factory=list, description="Structured diff hunks with unified/side-by-side lines.")
    unified_diff: str = Field(default="", description="Unified diff text of the server-side masked YAML.")


class CostEstimateOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    provider: str = Field(default="", description="Cloud provider priced (gke, eks, aks, or local).")
    currency: str = Field(default="USD", description="Currency code for monetary figures.")
    monthly_total_usd: Optional[float] = Field(
        default=None, description="Sum of estimated monthly compute costs across priced node pools (730 hrs/month)."
    )
    pools: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-node-pool estimates: name, machine_type, node_count, hourly_price_usd, monthly_estimate_usd, priced, and note.",
    )
    disclaimer: str = Field(
        default="", description="Mandatory disclaimer explaining that figures are public on-demand compute list estimates, not an invoice."
    )
    error: Optional[str] = Field(default=None, description="Error detail if pricing lookup failed.")


class EcosystemOverviewOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier queried.")
    detected_count: int = Field(default=0, description="Number of platform ecosystems detected as installed.")
    total_checked: int = Field(default=0, description="Total number of known platform ecosystems checked.")
    integrations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-ecosystem entries (`category`, `installed`, `total_count`, `resources` list of checked CRDs with per-CRD counts).",
    )


class AuditLogsOutput(_FlexibleOutput):
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Portal audit trail entries ordered newest-first with timestamp, user, action, cluster_id, namespace, resource, and details.",
    )
    count: int = Field(default=0, description="Number of audit log entries returned.")


class ClusterDiagnosticsOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier scanned.")
    namespace: Optional[str] = Field(
        default=None, description="Scoped namespace scanned, or null when scanning all non-system namespaces."
    )
    simulated: bool = Field(default=False, description="False for live cluster scans.")
    health_score: int = Field(default=100, description="Overall 0-100 cluster/namespace reliability health score.")
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="High-level RCA rollup: overall_score, active_root_causes, active_failures_count, workloads_healthy, and workloads_total.",
    )
    step_counts: Dict[str, int] = Field(
        default_factory=dict, description="Active failure counts keyed by `kubectl apply` pipeline step number (1-12)."
    )
    pipeline_stages: List[Dict[str, Any]] = Field(
        default_factory=list, description="Grouped 12-Step pipeline stage statuses (healthy, warning, critical) with failure and advisory counts."
    )
    components: List[Dict[str, Any]] = Field(
        default_factory=list, description="Per-Kubernetes-component (apiserver, scheduler, kubelet, probes, datapath) issue counts."
    )
    integrations: Dict[str, Any] = Field(
        default_factory=dict, description="Detected delivery/observability pipelines (Helm, Argo CD, Prometheus) for the scanned scope."
    )
    root_causes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Folded, ranked root causes with kind (active_failure/best_practice), step_number, severity, confidence, root_cause, affected resources, and remediation_cmd.",
    )
    total_issues: int = Field(default=0, description="Total unfolded raw findings detected.")
    folded_count: int = Field(default=0, description="Number of deduplicated root-cause groups.")
    raw_issues: List[Dict[str, Any]] = Field(
        default_factory=list, description="Unfolded individual findings underlying `root_causes`."
    )


class NetworkPolicySimulationOutput(_FlexibleOutput):
    cluster_id: str = Field(default="", description="Cluster identifier evaluated.")
    allowed: bool = Field(default=False, description="True only if both Egress from source pod and Ingress to destination pod are permitted.")
    verdict: str = Field(default="ALLOWED", description="Overall L3/L4 verdict: ALLOWED, BLOCKED_EGRESS, BLOCKED_INGRESS, or BLOCKED_BOTH.")
    egress_allowed: bool = Field(default=True, description="True if source pod Egress permits reaching the destination.")
    ingress_allowed: bool = Field(default=True, description="True if destination pod Ingress permits receiving traffic from the source.")
    egress_mode: str = Field(default="default-allow", description="Source Egress mode: 'default-allow' (no selecting policies) or 'policy-evaluated'.")
    ingress_mode: str = Field(default="default-allow", description="Destination Ingress mode: 'default-allow' (no selecting policies) or 'policy-evaluated'.")
    evaluated_port: Optional[int] = Field(default=None, description="Destination TCP/UDP/SCTP port evaluated, or null if unconstrained.")
    evaluated_protocol: str = Field(default="TCP", description="Transport protocol evaluated (TCP, UDP, or SCTP).")
    source: Dict[str, Any] = Field(default_factory=dict, description="Source pod metadata (`namespace`, `pod`, `pod_ip`, `labels`).")
    destination: Dict[str, Any] = Field(default_factory=dict, description="Destination pod/service metadata (`namespace`, `pod`, `service`, `pod_ip`, `labels`).")
    selecting_egress_policies: List[str] = Field(default_factory=list, description="Egress NetworkPolicies whose podSelector matches the source pod.")
    selecting_ingress_policies: List[str] = Field(default_factory=list, description="Ingress NetworkPolicies whose podSelector matches the destination pod.")
    allowing_egress_policies: List[str] = Field(default_factory=list, description="Subset of selecting Egress NetworkPolicies that permit this destination.")
    allowing_ingress_policies: List[str] = Field(default_factory=list, description="Subset of selecting Ingress NetworkPolicies that permit this source.")
    blocking_reasons: List[str] = Field(default_factory=list, description="Human-readable explanation of allowed or blocked NetworkPolicy paths.")
    suggested_fix_hint: str = Field(default="", description="Targeted NetworkPolicy rule hint when reachability is blocked.")


# --------------------------------------------------------------------------- #
# Tool metadata registry (title + parameter annotations + structured output)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    title: str
    params: Dict[str, Any]
    output_model: Type[BaseModel]


TOOL_SPECS: Dict[str, ToolSpec] = {
    "list_teams_and_clusters": ToolSpec(
        title="List Teams and Clusters",
        params={},
        output_model=TeamsAndClustersOutput,
    ),
    "get_fleet_overview": ToolSpec(
        title="Get Multi-Cluster Fleet Overview",
        params={},
        output_model=FleetOverviewOutput,
    ),
    "get_cluster_info": ToolSpec(
        title="Get Cluster Metadata and Info",
        params={"cluster_id": ClusterIdParam},
        output_model=ClusterInfoOutput,
    ),
    "get_nodes": ToolSpec(
        title="List Cluster Nodes",
        params={"cluster_id": ClusterIdParam},
        output_model=NodesOutput,
    ),
    "get_namespaces": ToolSpec(
        title="List Cluster Namespaces",
        params={"cluster_id": ClusterIdParam, "include_system": IncludeSystemParam},
        output_model=NamespacesOutput,
    ),
    "get_workloads_overview": ToolSpec(
        title="Get Workloads Health Overview",
        params={"cluster_id": ClusterIdParam, "namespace": OptionalNamespaceNonSystemParam},
        output_model=WorkloadsOverviewOutput,
    ),
    "get_namespace_resources": ToolSpec(
        title="Get Namespace Resource Inventory",
        params={"cluster_id": ClusterIdParam, "namespace": RequiredNamespaceParam},
        output_model=NamespaceResourcesOutput,
    ),
    "get_workloads_catalog": ToolSpec(
        title="Get Full Workloads Catalog",
        params={"cluster_id": ClusterIdParam, "namespace": OptionalNamespaceAllParam},
        output_model=WorkloadsCatalogOutput,
    ),
    "get_rollout_history": ToolSpec(
        title="Get Workload Rollout History",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "workload_type": WorkloadTypeParam,
            "name": WorkloadNameParam,
        },
        output_model=RolloutHistoryOutput,
    ),
    "get_applications": ToolSpec(
        title="Get Logical Applications",
        params={"cluster_id": ClusterIdParam, "namespace": OptionalNamespaceAllParam},
        output_model=ApplicationsOutput,
    ),
    "get_pod_details": ToolSpec(
        title="Inspect Pod Details and 12-Step Status",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "pod_name": PodNameParam,
        },
        output_model=PodDetailsOutput,
    ),
    "get_pod_logs": ToolSpec(
        title="Fetch Pod Container Logs",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "pod_name": PodNameParam,
            "container": ContainerNameParam,
            "tail_lines": TailLinesParam,
        },
        output_model=PodLogsOutput,
    ),
    "get_manifest": ToolSpec(
        title="Get Read-Only Resource YAML Manifest",
        params={
            "cluster_id": ClusterIdParam,
            "kind": ResourceKindParam,
            "name": ResourceNameParam,
            "namespace": ManifestNamespaceParam,
        },
        output_model=ManifestOutput,
    ),
    "get_network_topology": ToolSpec(
        title="Get Namespace Network Topology",
        params={"cluster_id": ClusterIdParam, "namespace": RequiredNamespaceParam},
        output_model=NetworkTopologyOutput,
    ),
    "get_storage_topology": ToolSpec(
        title="Get Namespace Storage Topology",
        params={"cluster_id": ClusterIdParam, "namespace": RequiredNamespaceParam},
        output_model=StorageTopologyOutput,
    ),
    "get_namespace_map": ToolSpec(
        title="Get Full Namespace Dependency Map",
        params={"cluster_id": ClusterIdParam, "namespace": RequiredNamespaceParam},
        output_model=NamespaceMapOutput,
    ),
    "get_cluster_workload_map": ToolSpec(
        title="Get Cluster Node-to-Pod Placement Map",
        params={"cluster_id": ClusterIdParam},
        output_model=ClusterWorkloadMapOutput,
    ),
    "get_events": ToolSpec(
        title="Get Live Kubernetes Events",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": OptionalNamespaceAllParam,
            "limit": EventsLimitParam,
        },
        output_model=EventsOutput,
    ),
    "get_extended_events": ToolSpec(
        title="Get Extended Events History",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": OptionalNamespaceAllParam,
            "limit": ExtendedEventsLimitParam,
        },
        output_model=ExtendedEventsOutput,
    ),
    "get_helm_releases": ToolSpec(
        title="List Installed Helm Releases",
        params={"cluster_id": ClusterIdParam, "namespace": OptionalNamespaceAllParam},
        output_model=HelmReleasesOutput,
    ),
    "get_helm_release_details": ToolSpec(
        title="Get Masked Helm Release Details",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "release_name": HelmReleaseNameParam,
            "revision": OptionalRevisionParam,
        },
        output_model=HelmReleaseDetailsOutput,
    ),
    "get_helm_release_history": ToolSpec(
        title="Get Helm Release Revision History",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "release_name": HelmReleaseNameParam,
            "max_revisions": MaxRevisionsParam,
        },
        output_model=HelmReleaseHistoryOutput,
    ),
    "diff_helm_revisions": ToolSpec(
        title="Diff Two Helm Release Revisions (Masked)",
        params={
            "cluster_id": ClusterIdParam,
            "namespace": RequiredNamespaceParam,
            "release_name": HelmReleaseNameParam,
            "from_revision": FromRevisionParam,
            "to_revision": ToRevisionParam,
            "mode": HelmDiffModeParam,
        },
        output_model=HelmDiffOutput,
    ),
    "get_cost_estimate": ToolSpec(
        title="Estimate Cluster Monthly Compute Cost",
        params={"cluster_id": ClusterIdParam},
        output_model=CostEstimateOutput,
    ),
    "get_ecosystem_overview": ToolSpec(
        title="Detect Installed Platform CRD Ecosystems",
        params={"cluster_id": ClusterIdParam},
        output_model=EcosystemOverviewOutput,
    ),
    "get_audit_logs": ToolSpec(
        title="Get KubeOrbit Portal Audit Logs",
        params={"cluster_id": OptionalClusterIdParam, "limit": AuditLimitParam},
        output_model=AuditLogsOutput,
    ),
    "diagnose_cluster_issues": ToolSpec(
        title="Diagnose Cluster Issues (12-Step RCA)",
        params={"cluster_id": ClusterIdParam, "namespace": OptionalNamespaceNonSystemParam},
        output_model=ClusterDiagnosticsOutput,
    ),
    "simulate_network_policy": ToolSpec(
        title="Simulate NetworkPolicy Reachability",
        params={
            "cluster_id": ClusterIdParam,
            "source_namespace": SourceNamespaceParam,
            "source_pod": SourcePodParam,
            "dest_namespace": DestNamespaceParam,
            "dest_pod": DestPodParam,
            "dest_service": DestServiceParam,
            "port": NetPolPortParam,
            "protocol": NetPolProtocolParam,
        },
        output_model=NetworkPolicySimulationOutput,
    ),
}


def build_mcp_tool_wrapper(fn: Callable[..., Any], spec: ToolSpec) -> Callable[..., Any]:
    """Return an async wrapper carrying rich MCP input/output annotations without
    mutating `fn` (so Google ADK can still consume the raw function directly)."""
    sig = inspect.signature(fn)
    new_params = []
    annotations: Dict[str, Any] = {}
    for p_name, param in sig.parameters.items():
        ann = spec.params.get(p_name, param.annotation)
        new_params.append(param.replace(annotation=ann))
        annotations[p_name] = ann
    annotations["return"] = spec.output_model

    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        return await fn(*args, **kwargs)

    _wrapped.__name__ = fn.__name__
    _wrapped.__qualname__ = fn.__qualname__
    _wrapped.__doc__ = inspect.cleandoc(fn.__doc__ or "")
    _wrapped.__module__ = fn.__module__
    _wrapped.__signature__ = sig.replace(parameters=new_params, return_annotation=spec.output_model)  # type: ignore[attr-defined]
    _wrapped.__annotations__ = annotations
    return _wrapped
