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

logger = logging.getLogger("kubeorbit.mcp")

mcp = FastMCP(
    name="kubeorbit",
    instructions=(
        "Read-only tools for the KubeOrbit multi-cloud Kubernetes portal "
        "(GKE/EKS/AKS/local). Every tool is a GET against KubeOrbit's own "
        "API -- none of them can create, modify, or delete anything in a "
        "cluster. Start with list_teams_and_clusters to find a cluster_id, "
        "then use get_fleet_overview or get_cluster_info for a starting "
        "point before drilling into namespaces/pods/topology."
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
    tools.get_cost_estimate,
    tools.get_ecosystem_overview,
    tools.get_audit_logs,
    tools.diagnose_cluster_issues,
]

for _fn in _TOOL_FUNCTIONS:
    mcp.tool(annotations=_READ_ONLY)(_fn)


def get_mcp_tool_catalog() -> list[dict]:
    """Return metadata for all read-only MCP tools exposed by KubeOrbit."""
    import inspect

    catalog: list[dict] = []
    for fn in _TOOL_FUNCTIONS:
        doc = inspect.getdoc(fn) or ""
        first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
        sig = inspect.signature(fn)
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
