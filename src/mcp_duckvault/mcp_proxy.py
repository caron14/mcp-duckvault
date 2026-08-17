"""stdio MCP surface that forwards every operation to the per-Vault daemon."""

import asyncio
import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .daemon import DaemonClient
from .errors import DuckVaultError


def create_proxy_server(client: DaemonClient) -> FastMCP:
    mcp = FastMCP("DuckVault-MCP")

    async def invoke(name: str, params: dict[str, object]) -> object:
        try:
            return await asyncio.to_thread(client.call, f"tool:{name}", params)
        except DuckVaultError as exc:
            raise ToolError(json.dumps(exc.as_dict(), separators=(",", ":"))) from exc

    @mcp.tool()
    async def search_notes(
        query: str, tag: Optional[str] = None, limit: int = 5
    ) -> dict[str, object]:
        """Search notes using vector similarity and an optional tag."""
        return await invoke("search_notes", {"query": query, "tag": tag, "limit": limit})

    @mcp.tool()
    async def list_recent_notes(days: int = 7, limit: int = 20) -> dict[str, object]:
        """List notes recently modified on disk."""
        return await invoke("list_recent_notes", {"days": days, "limit": limit})

    @mcp.tool()
    async def find_related_notes(path: str, depth: int = 1, limit: int = 10) -> dict[str, object]:
        """Find notes related through Markdown and OKF graph edges."""
        return await invoke("find_related_notes", {"path": path, "depth": depth, "limit": limit})

    @mcp.tool()
    async def list_graph_neighbors(path: str, depth: int = 1, limit: int = 20) -> dict[str, object]:
        """List graph nodes neighboring a Markdown note."""
        return await invoke("list_graph_neighbors", {"path": path, "depth": depth, "limit": limit})

    @mcp.tool()
    async def hybrid_search_notes(
        query: str,
        tag: Optional[str] = None,
        limit: int = 5,
        graph_depth: int = 1,
    ) -> dict[str, object]:
        """Combine vector similarity with graph expansion."""
        return await invoke(
            "hybrid_search_notes",
            {"query": query, "tag": tag, "limit": limit, "graph_depth": graph_depth},
        )

    @mcp.tool()
    async def search_okf_concepts(
        okf_type: Optional[str] = None, tag: Optional[str] = None, limit: int = 20
    ) -> dict[str, object]:
        """Search OKF Concept Documents by type and tag."""
        return await invoke(
            "search_okf_concepts", {"okf_type": okf_type, "tag": tag, "limit": limit}
        )

    @mcp.tool()
    async def explain_okf_concept(concept_id: str) -> dict[str, object]:
        """Explain an OKF concept and its graph relationships."""
        return await invoke("explain_okf_concept", {"concept_id": concept_id})

    @mcp.tool()
    async def get_index_status(
        include_failures: bool = False, failure_limit: int = 100
    ) -> dict[str, object]:
        """Return daemon readiness, index completeness, and optional failures."""
        status = await asyncio.to_thread(
            client.call,
            "status",
            {"failure_limit": failure_limit if include_failures else 0},
        )
        status["complete"] = status.get("index_state") == "ready"
        return status

    return mcp
