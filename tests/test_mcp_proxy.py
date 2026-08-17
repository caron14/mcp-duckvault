"""The stdio proxy preserves the public MCP tool surface."""

import asyncio

from mcp_duckvault.mcp_proxy import create_proxy_server


class FakeDaemonClient:
    def call(self, method, params=None):
        if method == "status":
            return {"state": "ready", "index_state": "ready", "failures": []}
        return f"called {method}"


def test_proxy_exposes_existing_tools_and_structured_index_status():
    server = create_proxy_server(FakeDaemonClient())
    names = {tool.name for tool in server._tool_manager.list_tools()}

    assert names == {
        "search_notes",
        "list_recent_notes",
        "find_related_notes",
        "list_graph_neighbors",
        "hybrid_search_notes",
        "search_okf_concepts",
        "explain_okf_concept",
        "get_index_status",
    }
    status = asyncio.run(
        server._tool_manager.call_tool("get_index_status", {}, convert_result=False)
    )
    assert status == {
        "state": "ready",
        "index_state": "ready",
        "failures": [],
        "complete": True,
    }
