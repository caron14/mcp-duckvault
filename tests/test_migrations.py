"""Schema migration and atomic reindex recovery tests."""

import duckdb
import pytest

from mcp_duckvault import cli
from mcp_duckvault.db_manager import SCHEMA_VERSION, DatabaseManager
from mcp_duckvault.errors import DuckVaultError
from mcp_duckvault.vault_identity import VaultIdentity, VaultLayout


class FakeEmbeddingModel:
    model_name = "test/model"

    def __init__(self, dimension=8, *, fail=False):
        self.dimension = dimension
        self.fail = fail

    def encode(self, texts, is_query=False):
        del is_query
        if self.fail:
            raise RuntimeError("intentional embedding failure")
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]


def create_versioned_database(path, identity, *, version=1, dimension=4):
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE system_config (key VARCHAR PRIMARY KEY, value VARCHAR)")
    configs = {
        "vault_id": identity.vault_id,
        "vault_path": str(identity.normalized_path),
        "schema_version": str(version),
        "embedding_dimension": str(dimension),
        "embedding_model_id": "test/old-model",
        "index_signature": "old-signature",
        "index_state": "ready",
    }
    conn.executemany("INSERT INTO system_config VALUES (?, ?)", configs.items())
    conn.execute("""
        CREATE TABLE documents (
            path VARCHAR PRIMARY KEY,
            md5 VARCHAR,
            metadata JSON,
            source_modified_at TIMESTAMP,
            index_signature VARCHAR,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(f"""
        CREATE TABLE chunks (
            chunk_id VARCHAR PRIMARY KEY,
            document_path VARCHAR,
            content TEXT,
            embedding FLOAT[{dimension}],
            metadata JSON
        )
    """)
    conn.execute(
        "INSERT INTO documents (path, md5, metadata, index_signature) "
        "VALUES ('old.md', 'old', '{}', 'old-signature')"
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('old', 'old.md', 'old content', ?, '{}')",
        [[1.0] + [0.0] * (dimension - 1)],
    )
    conn.close()


def test_schema_migration_creates_backup_and_preserves_data(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    identity = VaultIdentity.from_path(vault)
    db_path = tmp_path / "vault.db"
    create_versioned_database(db_path, identity)

    db = DatabaseManager(str(db_path), identity=identity)
    db.connect(load_vss=False)
    db.initialize_schema(embedding_dim=4, model_id="test/old-model")

    backup = db._config("last_migration_backup")
    assert db._config("schema_version") == str(SCHEMA_VERSION)
    assert db.conn.execute("SELECT content FROM chunks").fetchone() == ("old content",)
    assert backup is not None
    backups = list((tmp_path / "backups").glob("schema-v1-to-v2-*.db"))
    assert len(backups) == 1
    assert str(backups[0]) == backup


def test_pre_versioned_database_fixture_upgrades_through_every_migration(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    identity = VaultIdentity.from_path(vault)
    db_path = tmp_path / "vault.db"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE system_config (key VARCHAR PRIMARY KEY, value VARCHAR)")
    conn.executemany(
        "INSERT INTO system_config VALUES (?, ?)",
        {
            "vault_id": identity.vault_id,
            "vault_path": str(identity.normalized_path),
            "embedding_dimension": "4",
            "embedding_model_id": "test/old-model",
            "index_signature": "old-signature",
        }.items(),
    )
    conn.execute("""
        CREATE TABLE documents (
            path VARCHAR PRIMARY KEY,
            md5 VARCHAR,
            metadata JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE chunks (
            chunk_id VARCHAR PRIMARY KEY,
            document_path VARCHAR,
            content TEXT,
            embedding FLOAT[4],
            metadata JSON
        )
    """)
    conn.execute("INSERT INTO documents (path, md5, metadata) VALUES ('old.md', 'old', '{}')")
    conn.close()

    db = DatabaseManager(str(db_path), identity=identity)
    db.connect(load_vss=False)
    db.initialize_schema(embedding_dim=4, model_id="test/old-model")

    assert db._config("schema_version") == str(SCHEMA_VERSION)
    assert db._column_exists("documents", "source_modified_at")
    assert db._column_exists("documents", "index_signature")
    assert db.conn.execute("SELECT path FROM documents").fetchone() == ("old.md",)
    assert len(list((tmp_path / "backups").glob("schema-v0-to-v2-*.db"))) == 1


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    identity = VaultIdentity.from_path(vault)
    db_path = tmp_path / "vault.db"
    create_versioned_database(db_path, identity)
    original_apply = DatabaseManager._apply_migration

    def fail_after_schema_write(self, from_version, embedding_dim):
        del from_version, embedding_dim
        self.conn.execute("CREATE TABLE migration_should_rollback (value INTEGER)")
        raise RuntimeError("intentional migration failure")

    monkeypatch.setattr(DatabaseManager, "_apply_migration", fail_after_schema_write)
    db = DatabaseManager(str(db_path), identity=identity)
    db.connect(load_vss=False)
    with pytest.raises(DuckVaultError) as caught:
        db.initialize_schema(embedding_dim=4, model_id="test/old-model")
    assert caught.value.code == "MIGRATION_FAILED"
    assert db._config("schema_version") == "1"
    assert not db._table_exists("migration_should_rollback")
    db.close()

    monkeypatch.setattr(DatabaseManager, "_apply_migration", original_apply)
    retry = DatabaseManager(str(db_path), identity=identity)
    retry.connect(load_vss=False)
    retry.initialize_schema(embedding_dim=4, model_id="test/old-model")
    assert retry._config("schema_version") == str(SCHEMA_VERSION)
    assert retry.conn.execute("SELECT content FROM chunks").fetchone() == ("old content",)


def test_dimension_change_requires_atomic_reindex(tmp_path, monkeypatch, database_factory):
    monkeypatch.setenv("DUCKVAULT_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# replacement content", encoding="utf-8")
    layout = VaultLayout.for_vault(vault, create=True)
    db = database_factory(str(layout.db_path), identity=layout.identity, embedding_dim=4)
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata, index_signature) "
        "VALUES ('old.md', 'old', '{}', ?)",
        [db._config("index_signature")],
    )
    db.conn.execute(
        "INSERT INTO chunks VALUES ('old', 'old.md', 'old content', ?, '{}')",
        [[1.0, 0.0, 0.0, 0.0]],
    )
    db.close()

    changed = DatabaseManager(str(layout.db_path), identity=layout.identity)
    changed.connect(load_vss=False)
    changed.initialize_schema(embedding_dim=8, model_id="test/model")
    assert changed._config("index_state") == "reindex_required"
    assert changed.conn.execute("SELECT content FROM chunks").fetchone() == ("old content",)
    changed.close()

    result = cli._reindex_vault(str(vault), model=FakeEmbeddingModel(dimension=8), load_vss=False)
    rebuilt = DatabaseManager(str(layout.db_path), identity=layout.identity)
    rebuilt.connect(load_vss=False)
    assert result["status"] == "complete"
    assert rebuilt._config("index_state") == "ready"
    assert rebuilt.conn.execute("SELECT path FROM documents").fetchall() == [("note.md",)]
    assert (
        "FLOAT[8]"
        in rebuilt.conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'chunks' AND column_name = 'embedding'"
        ).fetchone()[0]
    )


def test_failed_reindex_preserves_old_index_and_recovery_details(
    tmp_path, monkeypatch, database_factory
):
    monkeypatch.setenv("DUCKVAULT_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# new content", encoding="utf-8")
    layout = VaultLayout.for_vault(vault, create=True)
    db = database_factory(str(layout.db_path), identity=layout.identity, embedding_dim=4)
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata, index_signature) "
        "VALUES ('old.md', 'old', '{}', ?)",
        [db._config("index_signature")],
    )
    db.close()

    with pytest.raises(DuckVaultError):
        cli._reindex_vault(str(vault), model=FakeEmbeddingModel(fail=True), load_vss=False)

    preserved = DatabaseManager(str(layout.db_path), identity=layout.identity)
    preserved.connect(load_vss=False)
    status = preserved.status()
    assert preserved.conn.execute("SELECT path FROM documents").fetchone() == ("old.md",)
    assert status["index_state"] == "reindex_failed"
    assert status["reindex"]["required"] is True
    assert status["reindex"]["backup"]
    assert status["reindex"]["repair"] == (f"duckvault reindex {layout.identity.normalized_path}")
    preserved.close()

    retry = cli._reindex_vault(str(vault), model=FakeEmbeddingModel(dimension=8), load_vss=False)
    assert retry["status"] == "complete"
    rebuilt = DatabaseManager(str(layout.db_path), identity=layout.identity)
    rebuilt.connect(load_vss=False)
    assert rebuilt._config("index_state") == "ready"
    assert rebuilt.conn.execute("SELECT path FROM documents").fetchone() == ("note.md",)
