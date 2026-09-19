"""Read-only MCP server for KubeOrbit.

Wraps a fixed allow-list of KubeOrbit's existing GET endpoints as MCP tools
so an agent (e.g. a Google ADK agent, see mcp_server/README.md) can answer
questions about clusters without ever gaining write access. See
mcp_server/tools.py for the exact, intentionally short list of what's
exposed, and mcp_server/README.md for what's deliberately left out and why.
"""
