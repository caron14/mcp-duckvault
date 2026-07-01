"""Tests for graph snapshots and the offline visualization artifact."""

import json

from click.testing import CliRunner

from mcp_duckvault import cli
from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.graph_extractor import GraphExtractor
from mcp_duckvault.graph_repository import GraphRepository
from mcp_duckvault.graph_visualizer import (
    build_graph_snapshot,
    render_graph_html,
    write_graph_visualization,
)


def _visualization_repository(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("orders.md", "customers.md", "isolated.md"):
        (vault / name).write_text("", encoding="utf-8")

    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)
    repository = GraphRepository(db)
    extractor = GraphExtractor(str(vault))
    documents = [
        (
            "orders.md",
            {
                "type": "table",
                "title": "Shared title",
                "tags": ["sales"],
                "resource": "https://example.com/orders",
            },
            "# Orders\n[Customers](customers.md)\n[Missing](missing.md)",
        ),
        (
            "customers.md",
            {"type": "table", "title": "Shared title", "tags": ["crm"]},
            "# Customers",
        ),
        (
            "isolated.md",
            {"title": "Isolated", "resource": "javascript:alert(1)"},
            "# Isolated",
        ),
    ]
    for path, metadata, body in documents:
        repository.upsert_document_graph(path, extractor.extract(path, metadata, body))
    return vault, repository


def test_build_graph_snapshot_and_diagnostics(tmp_path):
    vault, repository = _visualization_repository(tmp_path)

    snapshot = build_graph_snapshot(repository, vault, generated_at="2026-07-01T00:00:00+00:00")

    assert snapshot["schema_version"] == "1.0"
    assert snapshot["generated_at"] == "2026-07-01T00:00:00+00:00"
    assert snapshot["vault"] == {"name": "vault"}
    assert snapshot["stats"]["documents"] == 3
    assert [node["id"] for node in snapshot["nodes"]] == sorted(
        node["id"] for node in snapshot["nodes"]
    )
    orders = next(node for node in snapshot["nodes"] if node["id"] == "doc:orders.md")
    assert orders["display_type"] == "table"
    assert orders["tags"] == ["sales"]
    assert orders["resource_url"] == "https://example.com/orders"
    assert orders["initial_visible"]
    assert orders["out_degree"] == 1
    isolated = next(node for node in snapshot["nodes"] if node["id"] == "doc:isolated.md")
    assert isolated["resource_url"] is None
    assert {item["id"] for item in snapshot["diagnostics"]["orphan_nodes"]} == {"doc:isolated.md"}
    assert snapshot["diagnostics"]["dangling_links"][0]["id"] == "link_target:missing.md"
    assert snapshot["diagnostics"]["duplicate_titles"][0]["title"] == "Shared title"
    assert snapshot["diagnostics"]["high_degree_nodes"][0]["degree"] == 1


def test_rendered_html_is_offline_and_escapes_embedded_data(tmp_path):
    vault, repository = _visualization_repository(tmp_path)
    snapshot = build_graph_snapshot(repository, vault)
    snapshot["nodes"][0]["label"] = "</script><script>alert('xss')</script>"

    html = render_graph_html(snapshot)

    assert "<script src=" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "__GRAPH_DATA__" not in html
    assert "__CYTOSCAPE_JS__" not in html
    assert r"\u003c/script\u003e\u003cscript\u003ealert" in html
    assert "</script><script>alert('xss')</script>" not in html
    assert "Cytoscape.js 3.34.0" in html
    assert "Permission is hereby granted, free of charge" in html


def test_write_graph_visualization_creates_html_and_json(tmp_path):
    vault, repository = _visualization_repository(tmp_path)
    html_path = tmp_path / "output" / "graph.html"

    written_html, written_json = write_graph_visualization(repository, vault, html_path)

    assert written_html == html_path.resolve()
    assert written_json == html_path.with_suffix(".json").resolve()
    assert "<!doctype html>" in written_html.read_text(encoding="utf-8")
    graph = json.loads(written_json.read_text(encoding="utf-8"))
    assert graph["vault"]["name"] == "vault"
    assert graph["stats"]["documents"] == 3


def test_visualize_cli_syncs_writes_artifacts_and_exits(tmp_path, monkeypatch):
    class FakeEmbeddingModel:
        dimension = 4

        def encode(self, texts, is_query=False):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "---\ntype: concept\ntitle: Note\n---\n# Note\nBody", encoding="utf-8"
    )
    html_path = tmp_path / "artifacts" / "graph.html"
    monkeypatch.setattr(cli, "EmbeddingModel", FakeEmbeddingModel)

    result = CliRunner().invoke(
        cli.main,
        [
            str(vault),
            "--db-path",
            str(tmp_path / "vault.db"),
            "--visualize",
            "--output",
            str(html_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert html_path.exists()
    graph = json.loads(html_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert graph["stats"]["documents"] == 1
