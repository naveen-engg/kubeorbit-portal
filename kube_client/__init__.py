"""
Cloud-agnostic Kubernetes access layer.

Every provider (GKE/EKS/AKS) is only responsible for producing an isolated
kubeconfig file + kubectl context name (see providers/base.py). Once that
exists, *every* resource read in this package is 100% identical regardless of
which cloud is behind the cluster -- that is what makes the unified API/UX
requirement possible: app.py calls these functions the same way for every
provider, and they always return the same JSON-friendly shapes.

This package used to be a single ~2100-line kube_client.py. It's split by
concern (process execution, caching, the SDK-first/CLI-fallback fetch layer,
shape normalizers, per-resource-type fetchers, aggregate/topology views,
logs/YAML, audit, and RCA) so each piece -- and the read-only surface an MCP
server would wrap -- is easy to find and reason about on its own. This
`__init__.py` re-exports the full public API so every existing call site
(`import kube_client; kube_client.get_nodes(...)`, `from kube_client import
TTLCache`, etc.) keeps working unchanged.
"""
from __future__ import annotations

# -- process execution -------------------------------------------------------
from ._cli import (
    BinaryNotFoundError,
    CLIError,
    ResourceForbiddenError,
    ResourceTypeUnavailableError,
    run_cli,
)

# -- caching ------------------------------------------------------------------
from ._cache import TTLCache, cached

# -- kubeconfig / SDK helpers (used directly by providers/*.py) --------------
from ._sdk import kubeconfig_path_for_cluster, read_current_context

# -- kubectl invocation + cached SDK-first/CLI-fallback list helper ----------
from ._fetch import cached_kubectl_list, invalidate_cluster_cache, invalidate_cluster_data_cache, kubectl_json

# -- per-resource-type fetchers ------------------------------------------------
from .resources import (
    get_configmaps,
    get_cronjobs,
    get_daemonsets,
    get_deployments,
    get_events,
    get_gateways,
    get_helm_releases,
    get_httproutes,
    get_ingresses,
    get_jobs,
    get_namespaces,
    get_nodes,
    get_pods,
    get_pvcs,
    get_pvs,
    get_secrets_metadata,
    get_serviceaccounts,
    get_services,
    get_statefulsets,
    get_storageclasses,
)

# -- aggregate / topology views ------------------------------------------------
from .topology import (
    get_cluster_workload_map,
    get_namespace_map,
    get_network_topology,
    get_storage_topology,
    get_workloads_overview,
)

# -- logs / raw YAML -----------------------------------------------------------
from .logs import get_pod_logs, get_resource_yaml, is_secret_kind

# -- audit trail ----------------------------------------------------------------
from .audit import get_audit_logs, record_audit_event

# -- ecosystem CRD auto-discovery ------------------------------------------------
from .ecosystem import get_ecosystem_overview

# -- extended/historical events --------------------------------------------------
from .events import get_extended_events

# -- workload catalog, rollout history/rollback, logical apps, pod inspector ----
from .workloads import (
    get_logical_applications,
    get_pod_inspector,
    get_rollout_history,
    get_workloads_catalog,
    rollout_undo,
)

# -- workload change timeline & cross-cluster drift diff -----------------------
from .timeline import build_workload_timeline, get_workload_timeline
from .drift import compute_workload_drift, diff_workload_across_clusters

# -- root cause analysis ----------------------------------------------------------
from .rca import diagnose_cluster_issues

# -- static NetworkPolicy simulator ----------------------------------------------
from .netpol import (
    evaluate_network_policy_reachability,
    find_netpol_isolated_services,
    simulate_network_policy,
)

# -- helm release history, details & revision diff ------------------------------
from .helm import (
    diff_helm_release_revisions,
    get_helm_release_details,
    get_helm_release_history,
)

__all__ = [
    "BinaryNotFoundError",
    "CLIError",
    "ResourceForbiddenError",
    "ResourceTypeUnavailableError",
    "run_cli",
    "TTLCache",
    "cached",
    "kubeconfig_path_for_cluster",
    "read_current_context",
    "cached_kubectl_list",
    "invalidate_cluster_cache",
    "invalidate_cluster_data_cache",
    "kubectl_json",
    "get_configmaps",
    "get_cronjobs",
    "get_daemonsets",
    "get_deployments",
    "get_events",
    "get_gateways",
    "get_helm_releases",
    "get_helm_release_history",
    "get_helm_release_details",
    "diff_helm_release_revisions",
    "get_httproutes",
    "get_ingresses",
    "get_jobs",
    "get_namespaces",
    "get_nodes",
    "get_pods",
    "get_pvcs",
    "get_pvs",
    "get_secrets_metadata",
    "get_serviceaccounts",
    "get_services",
    "get_statefulsets",
    "get_storageclasses",
    "get_cluster_workload_map",
    "get_namespace_map",
    "get_network_topology",
    "get_storage_topology",
    "get_workloads_overview",
    "get_pod_logs",
    "get_resource_yaml",
    "is_secret_kind",
    "get_audit_logs",
    "record_audit_event",
    "get_ecosystem_overview",
    "get_extended_events",
    "get_logical_applications",
    "get_pod_inspector",
    "get_rollout_history",
    "get_workloads_catalog",
    "rollout_undo",
    "diagnose_cluster_issues",
    "build_workload_timeline",
    "get_workload_timeline",
    "compute_workload_drift",
    "diff_workload_across_clusters",
    "evaluate_network_policy_reachability",
    "find_netpol_isolated_services",
    "simulate_network_policy",
]
