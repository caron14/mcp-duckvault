"""Vault identity and database isolation tests."""

import os

import pytest

from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.errors import DuckVaultError, VaultIdentityError
from mcp_duckvault.vault_identity import VaultIdentity, VaultLayout


def test_equivalent_paths_and_symlink_share_identity(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(vault, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    direct = VaultIdentity.from_path(vault)
    dotted = VaultIdentity.from_path(vault / ".")
    linked = VaultIdentity.from_path(alias)

    assert direct == dotted == linked


def test_layout_separates_different_vaults(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKVAULT_HOME", str(tmp_path / "home"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_layout = VaultLayout.for_vault(first)
    second_layout = VaultLayout.for_vault(second)

    assert first_layout.identity.vault_id != second_layout.identity.vault_id
    assert first_layout.db_path != second_layout.db_path


def test_database_rejects_another_vault_before_schema_write(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    db_path = tmp_path / "shared.db"

    db = DatabaseManager(str(db_path), identity=VaultIdentity.from_path(first))
    db.initialize_schema(embedding_dim=4)
    db.close()

    wrong = DatabaseManager(str(db_path), identity=VaultIdentity.from_path(second))
    with pytest.raises(VaultIdentityError):
        wrong.initialize_schema(embedding_dim=4)


def test_database_rejects_newer_schema(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = tmp_path / "vault.db"
    identity = VaultIdentity.from_path(vault)
    db = DatabaseManager(str(db_path), identity=identity)
    db.initialize_schema(embedding_dim=4)
    db.set_config("schema_version", 999)
    db.close()

    newer = DatabaseManager(str(db_path), identity=identity)
    with pytest.raises(DuckVaultError, match="newer than supported"):
        newer.initialize_schema(embedding_dim=4)


def test_checkpointed_backup_preserves_source_and_contents(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = tmp_path / "vault.db"
    backup_path = tmp_path / "backups" / "vault.db"
    db = DatabaseManager(str(db_path), identity=VaultIdentity.from_path(vault))
    db.initialize_schema(embedding_dim=4)
    db.conn.execute("INSERT INTO documents (path, md5, metadata) VALUES ('note.md', 'hash', '{}')")

    db.backup(backup_path)

    assert db_path.exists()
    assert backup_path.exists()
    backup = DatabaseManager(str(backup_path), read_only=True)
    backup.connect(load_vss=False)
    assert backup.conn.execute("SELECT path FROM documents").fetchone() == ("note.md",)
    backup.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows path case normalization")
def test_windows_path_case_is_normalized(tmp_path):
    vault = tmp_path / "MixedCase"
    vault.mkdir()
    assert VaultIdentity.from_path(vault) == VaultIdentity.from_path(str(vault).swapcase())
