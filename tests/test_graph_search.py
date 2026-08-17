"""End-to-end tests for MCP graph and hybrid retrieval tools."""

import asyncio
import json

from mcp_duckvault.graph_extractor import GraphExtractor
from mcp_duckvault.graph_repository import GraphRepository
from mcp_duckvault.mcp_server import create_mcp_server


class QueryModel:
    def encode(self, texts, is_query=False):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_graph_and_hybrid_mcp_tools(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "orders.md").write_text("", encoding="utf-8")
    (vault / "customers.md").write_text("", encoding="utf-8")

    db = database_factory()
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
        return structured

    async def verify():
        search = await call("search_notes", {"query": "orders"})
        assert {item["path"] for item in search["items"]} == {"orders.md", "customers.md"}
        related = await call("find_related_notes", {"path": "orders.md"})
        assert "customers.md" in {item["document_path"] for item in related["items"]}
        neighbors = await call("list_graph_neighbors", {"path": "orders.md"})
        assert "HAS_TYPE" in {item["edge_type"] for item in neighbors["items"]}
        hybrid = await call("hybrid_search_notes", {"query": "orders"})
        assert all("score" in item for item in hybrid["items"])
        concepts = await call("search_okf_concepts", {"okf_type": "table"})
        assert [item["title"] for item in concepts["items"]] == ["Orders"]
        explanation = await call("explain_okf_concept", {"concept_id": "orders"})
        relationships = (
            explanation["items"][0]["linked_concepts"] + explanation["items"][0]["related_notes"]
        )
        assert "customers.md" in {item["document_path"] for item in relationships}

    asyncio.run(verify())
