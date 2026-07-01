"""End-to-end tests for MCP graph and hybrid retrieval tools."""

import asyncio
import json

from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.graph_extractor import GraphExtractor
from mcp_duckvault.graph_repository import GraphRepository
from mcp_duckvault.mcp_server import create_mcp_server


class QueryModel:
    def encode(self, texts, is_query=False):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_graph_and_hybrid_mcp_tools(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "orders.md").write_text("", encoding="utf-8")
    (vault / "customers.md").write_text("", encoding="utf-8")

    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)
    extractor = GraphExtractor(str(vault))
    repository = GraphRepository(db)
    documents = [
        (
            "orders.md",
            {"type": "table", "title": "Orders", "tags": ["sales"]},
            "# Orders\n[Customers](./customers.md)",
        ),
        ("customers.md", {"title": "Customers"}, "# Customers"),
    ]
    for path, metadata, content in documents:
        db.conn.execute(
            "INSERT INTO documents (path, md5, metadata) VALUES (?, ?, ?)",
            (path, path, json.dumps(metadata)),
        )
        db.conn.execute(
            """
            INSERT INTO chunks (chunk_id, document_path, content, embedding)
            VALUES (?, ?, ?, ?)
            """,
            (path, path, content, [1.0, 0.0, 0.0, 0.0]),
        )
        repository.upsert_document_graph(path, extractor.extract(path, metadata, content))

    server = create_mcp_server(str(vault), db, QueryModel())

    async def call(name, arguments):
        _, structured = await server.call_tool(name, arguments)
        return structured["result"]

    async def verify():
        assert "orders.md" in await call("search_notes", {"query": "orders"})
        assert "customers.md" in await call("find_related_notes", {"path": "orders.md"})
        assert "HAS_TYPE" in await call("list_graph_neighbors", {"path": "orders.md"})
        assert "Graph:" in await call("hybrid_search_notes", {"query": "orders"})
        assert "Orders" in await call("search_okf_concepts", {"okf_type": "table"})
        assert "customers.md" in await call("explain_okf_concept", {"concept_id": "orders"})

    asyncio.run(verify())
