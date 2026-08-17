"""Stable structured MCP result and error contracts."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_duckvault.mcp_server import MAX_SNIPPET_CHARS, create_mcp_server


class TrackingModel:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def encode(self, texts, is_query=False):
        del texts, is_query
        self.calls += 1
        if self.fail:
            raise RuntimeError("private internal detail")
        return [[1.0, 0.0, 0.0, 0.0]]


def call(server, name, arguments):
    async def invoke():
        _, structured = await server.call_tool(name, arguments)
        return structured

    return asyncio.run(invoke())


def test_search_contract_deduplicates_documents_and_bounds_snippets(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    db = database_factory()
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata) VALUES ('note.md', 'hash', ?)",
        [json.dumps({"tags": ["Work"]})],
    )
    db.conn.execute(
        "INSERT INTO chunks VALUES ('low', 'note.md', 'low', ?, '{}')",
        [[0.0, 1.0, 0.0, 0.0]],
    )
    db.conn.execute(
        "INSERT INTO chunks VALUES ('best', 'note.md', ?, ?, '{}')",
        ["# Best heading\n" + "x" * (MAX_SNIPPET_CHARS + 100), [1.0, 0.0, 0.0, 0.0]],
    )
    server = create_mcp_server(str(vault), db, TrackingModel())

    result = call(server, "search_notes", {"query": "test", "tag": "#work"})

    assert result["schema_version"] == "1.0"
    assert result["tool"] == "search_notes"
    assert result["count"] == 1
    assert result["items"][0]["heading"] == "Best heading"
    assert len(result["items"][0]["snippet"]) == MAX_SNIPPET_CHARS
    assert result["items"][0]["reason"] == "best_vector_chunk"


def test_empty_search_result_uses_the_same_schema(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    server = create_mcp_server(str(vault), database_factory(), TrackingModel())

    result = call(server, "search_notes", {"query": "nothing"})

    assert result == {
        "schema_version": "1.0",
        "tool": "search_notes",
        "count": 0,
        "items": [],
        "query": "nothing",
        "tag": None,
    }


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_notes", {"query": ""}),
        ("search_notes", {"query": "ok", "limit": 101}),
        ("list_recent_notes", {"days": -1}),
        ("find_related_notes", {"path": " ", "depth": 1}),
        ("list_graph_neighbors", {"path": "note.md", "depth": 6}),
        ("hybrid_search_notes", {"query": "ok", "graph_depth": 0}),
        ("search_okf_concepts", {"limit": 0}),
        ("explain_okf_concept", {"concept_id": ""}),
        ("get_index_status", {"failure_limit": 101}),
    ],
)
def test_invalid_arguments_have_stable_errors_before_model_execution(
    tmp_path, database_factory, tool, arguments
):
    vault = tmp_path / "vault"
    vault.mkdir()
    model = TrackingModel()
    server = create_mcp_server(str(vault), database_factory(), model)

    with pytest.raises(ToolError, match="INVALID_ARGUMENT|ARGUMENT_TOO_LARGE"):
        call(server, tool, arguments)

    assert model.calls == 0


def test_internal_error_uses_stable_public_code(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    server = create_mcp_server(str(vault), database_factory(), TrackingModel(fail=True))

    with pytest.raises(ToolError) as caught:
        call(server, "search_notes", {"query": "test"})

    assert "SEARCH_FAILED" in str(caught.value)
    assert "private internal detail" not in str(caught.value)


def test_recent_notes_use_source_modified_at_and_limit(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    db = database_factory()
    now = datetime.now()
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata, source_modified_at) VALUES (?, ?, '{}', ?)",
        ("recent.md", "recent", now),
    )
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata, source_modified_at) VALUES (?, ?, '{}', ?)",
        ("old.md", "old", now - timedelta(days=30)),
    )
    server = create_mcp_server(str(vault), db, TrackingModel())

    result = call(server, "list_recent_notes", {"days": 7, "limit": 1})

    assert [item["path"] for item in result["items"]] == ["recent.md"]
