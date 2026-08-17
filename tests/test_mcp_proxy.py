"""The stdio proxy preserves the public MCP tool surface."""

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_duckvault.errors import DuckVaultError
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


def test_proxy_preserves_structured_daemon_error_fields():
    class FailingClient:
        def call(self, method, params=None):
            del method, params
            raise DuckVaultError("INVALID_ARGUMENT", "bad input", retryable=False)

    server = create_proxy_server(FailingClient())
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server._tool_manager.call_tool(
                "search_notes", {"query": "test"}, convert_result=False
            )
        )

    message = str(caught.value)
    assert '"code":"INVALID_ARGUMENT"' in message
    assert '"message":"bad input"' in message
    assert '"retryable":false' in message
