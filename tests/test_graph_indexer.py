"""Integration tests for graph updates in the vault indexer."""

from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.indexer import VaultIndexer


class FakeEmbeddingModel:
    def encode(self, texts, is_query=False):
        return [[float(len(text)), 0.0, 0.0, 1.0] for text in texts]


def test_index_update_and_delete_are_atomic_for_graph_data(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("---\ntype: concept\ntags: [one]\n---\n# Old\nBody", encoding="utf-8")

    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)
    indexer = VaultIndexer(str(vault), db, model=FakeEmbeddingModel())
    indexer.index_file(str(note))

    assert (
        db.conn.execute("SELECT node_type FROM nodes WHERE node_id = 'doc:note.md'").fetchone()[0]
        == "okf_concept"
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM nodes WHERE node_id = 'heading:note.md#old'"
        ).fetchone()[0]
        == 1
    )

    note.write_text("---\ntype: concept\ntags: [two]\n---\n# New\nBody", encoding="utf-8")
    indexer.index_file(str(note))
    assert (
        db.conn.execute(
            "SELECT count(*) FROM nodes WHERE node_id = 'heading:note.md#old'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM nodes WHERE node_id = 'heading:note.md#new'"
        ).fetchone()[0]
        == 1
    )

    indexer.delete_file(str(note))
    assert db.conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 0
