"""Tests for Markdown and OKF graph extraction."""

from mcp_duckvault.graph_extractor import GraphExtractor


def test_extracts_markdown_and_okf_structure(tmp_path):
    vault = tmp_path / "vault"
    (vault / "tables").mkdir(parents=True)
    (vault / "metrics").mkdir()
    (vault / "datasets").mkdir()
    for path in (
        vault / "tables" / "orders.md",
        vault / "tables" / "customers.md",
        vault / "metrics" / "revenue.md",
        vault / "datasets" / "sales.md",
    ):
        path.write_text("", encoding="utf-8")

    metadata = {
        "type": "table",
        "title": "Orders",
        "description": "Customer orders",
        "tags": ["Analytics", "sales"],
        "timestamp": "2026-07-01",
        "resource": {"name": "warehouse.orders", "url": "duckdb:///warehouse"},
        "owner": "data",
    }
    body = """# Orders
## Columns
[[customers]]
[[customers|Customer table]]
[Revenue](/metrics/revenue.md)
[Sales](../datasets/sales.md)
[Missing](./missing.md)
[Source](https://example.com/orders)
#inline-tag
"""
    graph = GraphExtractor(str(vault)).extract("tables/orders.md", metadata, body)
    nodes = {node.node_id: node for node in graph.nodes}
    edges = {(edge.edge_type, edge.target_node_id) for edge in graph.edges}

    assert nodes["doc:tables/orders.md"].node_type == "okf_concept"
    assert nodes["doc:tables/orders.md"].name == "Orders"
    assert nodes["doc:tables/orders.md"].metadata["concept_id"] == "tables/orders"
    assert nodes["heading:tables/orders.md#orders"].metadata["level"] == 1
    assert "tag:analytics" in nodes
    assert "tag:inline-tag" in nodes
    assert "folder:tables" in nodes
    assert ("HAS_TYPE", "okf_type:table") in edges
    assert "resource:duckdb:///warehouse" in nodes
    assert any(edge_type == "DESCRIBES_RESOURCE" for edge_type, _ in edges)
    assert ("LINKS_TO", "doc:tables/customers.md") in edges
    assert ("MENTIONS_LINK", "doc:tables/customers.md") in edges
    assert ("LINKS_TO", "doc:metrics/revenue.md") in edges
    assert ("LINKS_TO", "doc:datasets/sales.md") in edges
    assert ("LINKS_TO", "link_target:tables/missing.md") in edges
    assert any(edge_type == "CITES_SOURCE" for edge_type, _ in edges)


def test_reserved_okf_files_are_not_concepts():
    extractor = GraphExtractor()

    index = extractor.extract("tables/index.md", {"type": "index"}, "")
    log = extractor.extract("tables/log.md", {"type": "log"}, "")

    assert index.nodes[0].node_type == "okf_index"
    assert log.nodes[0].node_type == "okf_log"
    assert "concept_id" not in index.nodes[0].metadata
    assert GraphExtractor.concept_id("tables/orders.md") == "tables/orders"
