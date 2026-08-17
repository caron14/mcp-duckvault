"""Shared hermetic test fixtures."""

from collections.abc import Callable

import pytest

from mcp_duckvault.db_manager import DEFAULT_MODEL_ID, DatabaseManager
from mcp_duckvault.vault_identity import VaultIdentity


@pytest.fixture(autouse=True)
def forbid_vss_in_hermetic_tests(request, monkeypatch):
    """Fail fast if an unmarked test tries to load or install VSS."""
    if request.node.get_closest_marker("vss") is not None:
        return

    original_connect = DatabaseManager.connect

    def connect_without_vss(self, *, load_vss=True):
        if load_vss:
            raise AssertionError("Hermetic tests must connect with load_vss=False")
        return original_connect(self, load_vss=False)

    def reject_vss_install(_db_path):
        raise AssertionError("Hermetic tests must not install VSS")

    monkeypatch.setattr(DatabaseManager, "connect", connect_without_vss)
    monkeypatch.setattr(DatabaseManager, "prepare_vss", reject_vss_install)


@pytest.fixture
def database_factory() -> Callable[..., DatabaseManager]:
    """Create an initialized DuckDB database without loading or installing VSS."""

    def create(
        db_path: str = ":memory:",
        *,
        identity: VaultIdentity | None = None,
        embedding_dim: int = 4,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> DatabaseManager:
        db = DatabaseManager(db_path, identity=identity)
        db.connect(load_vss=False)
        db.initialize_schema(embedding_dim=embedding_dim, model_id=model_id)
        return db

    return create
