"""Builds the KubeOrbit MCP server and runs it over stdio or streamable-HTTP.

    python -m mcp_server.server              # stdio (spawned by an agent)
    KUBEORBIT_MCP_TRANSPORT=streamable-http python -m mcp_server.server

See mcp_server/README.md for how to point a Google ADK agent (or any other
MCP client) at this.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import tools
from .config import get_settings
from .schemas import TOOL_SPECS, build_mcp_tool_wrapper

logger = logging.getLogger("kubeorbit.mcp")

mcp = FastMCP(
    name="kubeorbit",
    instructions=(
        "Strictly read-only MCP tools for the KubeOrbit multi-cloud Kubernetes diagnostics portal "
        "(GCP GKE, AWS EKS, Azure AKS, and local Kubernetes). Every tool issues an HTTP GET against "
        "KubeOrbit's REST API -- zero cluster writes, zero Secret value exposure (`get_manifest` "
        "hard-blocks Secret objects with HTTP 403, resource lists expose Secret metadata only, and "
        "Helm values/diffs mask secrets server-side with SHA-256 fingerprints).\n\n"
        "Recommended routing:\n"
        "- Discovery: Call `list_teams_and_clusters` first to obtain a `cluster_id`, or `get_fleet_overview` for live multi-cluster health.\n"
        "- Triage: Start with `diagnose_cluster_issues` (12-Step RCA) or `get_workloads_overview` (counts), then drill into `get_pod_details` / `get_pod_logs` / `get_manifest`.\n"
        "- Workloads: Use `get_workloads_overview` for counts, `get_workloads_catalog` for full lists, and `get_applications` for app-grouped views.\n"
        "- Namespace views: Use `get_namespace_resources` for flat tables, `get_namespace_map` for full dependency graphs, `get_network_topology` for ingress/Gateway/Service->Pod traffic, `simulate_network_policy` to test L3/L4 pod-to-pod/Service NetworkPolicy reachability, and `get_storage_topology` for StorageClass/PV/PVC->Pod storage.\n"
        "- Events & Audit: Use `get_events` for live K8s API events, `get_extended_events` for live + in-memory history with warning counts, and `get_audit_logs` for KubeOrbit portal audit actions.\n"
        "- Revisions & Helm: Use `get_rollout_history` for native K8s Deployment/StatefulSet/DaemonSet revisions; use `get_helm_releases`, `get_helm_release_history`, `get_helm_release_details`, and `diff_helm_revisions` for Helm charts."
    ),
)

# Every tool is registered with readOnlyHint=True/destructiveHint=False so
# any MCP client that honors tool annotations (including ADK) can see the
# read-only guarantee without inspecting source -- it isn't just a docstring
# claim. `tools.py` backs this up structurally: its KubeOrbitClient has no
# method to issue anything but a GET.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

_TOOL_FUNCTIONS = [
    tools.list_teams_and_clusters,
    tools.get_fleet_overview,
    tools.get_cluster_info,
    tools.get_nodes,
    tools.get_namespaces,
    tools.get_workloads_overview,
    tools.get_namespace_resources,
    tools.get_workloads_catalog,
    tools.get_rollout_history,
    tools.get_applications,
    tools.get_pod_details,
    tools.get_pod_logs,
    tools.get_manifest,
    tools.get_network_topology,
    tools.get_storage_topology,
    tools.get_namespace_map,
    tools.get_cluster_workload_map,
    tools.get_events,
    tools.get_extended_events,
    tools.get_helm_releases,
    tools.get_helm_release_details,
    tools.get_helm_release_history,
    tools.diff_helm_revisions,
    tools.get_cost_estimate,
    tools.get_ecosystem_overview,
    tools.get_audit_logs,
    tools.diagnose_cluster_issues,
    tools.simulate_network_policy,
]

for _fn in _TOOL_FUNCTIONS:
    _spec = TOOL_SPECS[_fn.__name__]
    _wrapped = build_mcp_tool_wrapper(_fn, _spec)
    mcp.tool(
        title=_spec.title,
        annotations=_READ_ONLY,
        structured_output=True,
    )(_wrapped)


def get_mcp_tool_catalog() -> list[dict]:
    """Return metadata for all read-only MCP tools exposed by KubeOrbit."""
    import inspect

    catalog: list[dict] = []
    for fn in _TOOL_FUNCTIONS:
        doc = inspect.getdoc(fn) or ""
        first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
        sig = inspect.signature(fn)
        spec = TOOL_SPECS[fn.__name__]
        params = [
            {
                "name": p_name,
                "required": param.default is inspect.Parameter.empty,
            }
            for p_name, param in sig.parameters.items()
        ]
        catalog.append(
            {
                "name": fn.__name__,
                "title": spec.title,
                "description": first_line,
                "parameters": params,
                "read_only": True,
                "destructive": False,
                "protocol": "mcp",
            }
        )
    return catalog


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    logger.info(
        "Starting KubeOrbit MCP server: transport=%s target=%s tools=%d",
        settings.transport, settings.kubeorbit_base_url, len(_TOOL_FUNCTIONS),
    )
    if settings.transport == "streamable-http":
        mcp.settings.host = settings.host
        mcp.settings.port = settings.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
