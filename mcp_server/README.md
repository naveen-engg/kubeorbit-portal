# KubeOrbit MCP Server

A read-only [MCP](https://modelcontextprotocol.io) server that wraps KubeOrbit's
existing REST API as tools an AI agent can call. It never touches
`kubectl`/cloud CLIs itself -- every tool is a plain `GET` against the
already-running KubeOrbit app, so it inherits KubeOrbit's own cluster auth,
caching, and audit logging for free.

## Read-only, structurally

- `mcp_server/client.py`'s `KubeOrbitClient` has a `get()` method and nothing
  else -- no `post`/`put`/`delete`. A new tool can only be wired to a GET.
- `mcp_server/tools.py` is the complete, one-file list of everything exposed.
  It deliberately does **not** include secret reveal, rollback, or cluster
  admin (add/delete) -- those stay only in the KubeOrbit UI/API, gated by
  KubeOrbit's own auth and audit log.
- Every tool is registered with `readOnlyHint=True` / `destructiveHint=False`
  MCP annotations, so a compliant client (including an ADK agent) can see the
  guarantee without reading source.

## Running it

KubeOrbit itself must already be running (`uvicorn app:app`). Then either:

```bash
# stdio -- for an agent that spawns this as a subprocess (e.g. local dev)
python -m mcp_server.server

# streamable-http -- run as its own standing service
KUBEORBIT_MCP_TRANSPORT=streamable-http python -m mcp_server.server
```

### Configuration (environment variables)

| Variable | Default | Purpose |
| :-- | :-- | :-- |
| `KUBEORBIT_BASE_URL` | `http://localhost:8080` | Where KubeOrbit's API is reachable |
| `KUBEORBIT_API_KEY` | unset | Sent as `X-API-Key` when KubeOrbit runs with `KUBEORBIT_AUTH_MODE=api_key` |
| `KUBEORBIT_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `KUBEORBIT_MCP_HOST` | `127.0.0.1` | Bind host (streamable-http only) |
| `KUBEORBIT_MCP_PORT` | `8090` | Bind port (streamable-http only) |
| `KUBEORBIT_MCP_TIMEOUT_SECONDS` | `30` | Per-request timeout to KubeOrbit |

## Connecting a Google ADK agent

```python
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

kubeorbit_tools = MCPToolset(
    connection_params=StdioServerParameters(
        command="python",
        args=["-m", "mcp_server.server"],
        env={"KUBEORBIT_BASE_URL": "http://localhost:8080"},
    ),
)

agent = LlmAgent(
    model="gemini-2.0-flash",
    name="kubeorbit_agent",
    instruction=(
        "You help operators understand the state of their Kubernetes clusters "
        "through KubeOrbit. You only have read-only tools -- you cannot change "
        "anything in a cluster. Start with list_teams_and_clusters to find a "
        "cluster_id before calling other tools."
    ),
    tools=[kubeorbit_tools],
)
```

(Swap `StdioServerParameters` for ADK's streamable-HTTP connection params if
you run this server with `KUBEORBIT_MCP_TRANSPORT=streamable-http` as a
separate service instead of a spawned subprocess. Check the installed
`google-adk` version's docs for the exact class name/import path -- this has
moved across ADK releases.)

## Tool list

See the docstring on each function in `tools.py` for exact parameters,
`list_teams_and_clusters` / `get_fleet_overview` for the recommended entry
points, cluster/namespace/pod fetchers (`get_nodes`, `get_namespaces`,
`get_namespace_resources`, `get_pod_details`, `get_pod_logs`, `get_manifest`,
...), topology graphs (`get_network_topology`, `get_storage_topology`,
`get_namespace_map`, `get_cluster_workload_map`), and
`diagnose_cluster_issues` for KubeOrbit's root-cause-analysis scan.
