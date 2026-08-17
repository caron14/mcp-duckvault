"""Synchronization aggregation and failure visibility tests."""

from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.indexer import VaultIndexer


class FakeEmbeddingModel:
    def encode(self, texts, is_query=False):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_full_sync_reports_partial_and_persists_safe_failure(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "good.md").write_text("# Good\nBody", encoding="utf-8")
    (vault / "bad.md").write_bytes(b"\xff\xfe")
    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)

    summary = VaultIndexer(str(vault), db, model=FakeEmbeddingModel()).full_sync()

    assert summary.status == "partial"
    assert summary.scanned == 2
    assert summary.indexed == 1
    assert summary.failed == 1
    status = db.status()
    assert status["index_state"] == "degraded"
    assert status["failures"][0]["path"] == "bad.md"
    assert status["failures"][0]["error_code"] == "INDEX_FILE_FAILED"
    assert "Body" not in str(status["failures"])


def test_successful_retry_clears_current_failure(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_bytes(b"\xff")
    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)
    indexer = VaultIndexer(str(vault), db, model=FakeEmbeddingModel())
    assert indexer.full_sync().status == "failed"

    note.write_text("# Fixed", encoding="utf-8")
    assert indexer.full_sync().status == "complete"
    assert db.status()["failures"] == []


def test_deleting_never_indexed_failure_clears_it(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "bad.md"
    note.write_bytes(b"\xff")
    db = DatabaseManager(":memory:")
    db.initialize_schema(embedding_dim=4)
    indexer = VaultIndexer(str(vault), db, model=FakeEmbeddingModel())
    assert indexer.full_sync().status == "failed"

    note.unlink()
    assert indexer.full_sync().status == "complete"
    assert db.status()["failures"] == []
