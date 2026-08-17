"""Tests for graph persistence, traversal, filtering, and cleanup."""

from mcp_duckvault.graph_extractor import GraphExtractor
from mcp_duckvault.graph_repository import GraphRepository


def test_repository_persists_and_queries_okf_graph(tmp_path, database_factory):
    vault = tmp_path / "vault"
    (vault / "tables").mkdir(parents=True)
    orders_path = vault / "tables" / "orders.md"
    customers_path = vault / "tables" / "customers.md"
    orders_path.write_text("", encoding="utf-8")
    customers_path.write_text("", encoding="utf-8")

    extractor = GraphExtractor(str(vault))
    orders = extractor.extract(
        "tables/orders.md",
        {
            "type": "table",
            "title": "Orders",
            "description": "Orders table",
            "tags": ["sales"],
            "resource": "warehouse.orders",
        },
        "# Orders\n[Customers](./customers.md)",
    )
    customers = extractor.extract(
        "tables/customers.md",
        {"type": "table", "title": "Customers", "tags": ["crm"]},
        "# Customers",
    )

    db = database_factory()
    repository = GraphRepository(db)
    repository.upsert_document_graph("tables/orders.md", orders)
    repository.upsert_document_graph("tables/customers.md", customers)

    concept = repository.find_okf_concept("tables/orders")
    assert concept["title"] == "Orders"
    assert concept["type"] == "table"
    assert concept["resource"] == "warehouse.orders"
    assert concept["tags"] == ["sales"]
    assert concept["linked_concepts"][0]["document_path"] == "tables/customers.md"

    results = repository.search_okf_concepts(okf_type="TABLE", tag="#sales")
    assert [result["concept_id"] for result in results] == ["tables/orders"]

    neighbors = repository.find_neighbors("tables/orders.md")
    assert any(item["document_path"] == "tables/customers.md" for item in neighbors)

    repository.delete_document_graph("tables/orders.md")
    assert repository.find_okf_concept("tables/orders") is None
    assert (
        db.conn.execute(
            "SELECT count(*) FROM edges WHERE document_path = 'tables/orders.md'"
        ).fetchone()[0]
        == 0
    )
    assert repository.find_okf_concept("tables/customers") is not None


def test_garbage_collection_removes_unreferenced_shared_nodes(database_factory):
    db = database_factory()
    repository = GraphRepository(db)
    graph = GraphExtractor().extract("note.md", {"tags": ["temporary"]}, "")
    repository.upsert_document_graph("note.md", graph)
    repository.delete_document_graph("note.md")
    repository.collect_garbage()

    assert db.conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 0
