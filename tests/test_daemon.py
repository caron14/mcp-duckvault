"""Local daemon ownership and concurrent-client tests."""

import concurrent.futures
import threading
import time

import pytest

from mcp_duckvault.daemon import DaemonClient, DuckVaultDaemon, ensure_daemon
from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.errors import DuckVaultError
from mcp_duckvault.indexer import VaultIndexer
from mcp_duckvault.vault_identity import VaultLayout


class FakeEmbeddingModel:
    def encode(self, texts, is_query=False):
        return [[1.0] + [0.0] * 383 for _ in texts]


def test_daemon_requires_explicit_initialization(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKVAULT_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(DuckVaultError, match="duckvault init"):
        ensure_daemon(VaultLayout.for_vault(vault), timeout=0.1)


def test_three_clients_share_one_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKVAULT_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    layout = VaultLayout.for_vault(vault, create=True)
    DatabaseManager.prepare_vss(str(layout.db_path))
    db = DatabaseManager(str(layout.db_path), identity=layout.identity)
    db.initialize_schema()
    note = vault / "note.md"
    note.write_text("# Concurrent watcher update", encoding="utf-8")
    VaultIndexer(str(vault), db, model=FakeEmbeddingModel()).full_sync()
    db.close()

    daemon = DuckVaultDaemon(layout)
    thread = threading.Thread(target=daemon.run)
    thread.start()
    client = DaemonClient(layout, timeout=2)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if client.health()["state"] == "ready":
                break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        raise AssertionError("daemon did not become ready")

    wrong_db = tmp_path / "wrong.db"
    wrong_db.touch()
    with pytest.raises(DuckVaultError, match="Running daemon uses"):
        ensure_daemon(layout, db_path=str(wrong_db))

    second = DuckVaultDaemon(layout)
    with pytest.raises(DuckVaultError, match="already owns"):
        second.run()
    second.server.server_close()

    clients = [DaemonClient(layout, timeout=2) for _ in range(3)]
    original_submit = daemon.worker.submit
    daemon.worker.submit = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("status must not wait on the DB worker")
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        statuses = list(executor.map(lambda item: item.call("status"), clients))
    daemon.worker.submit = original_submit

    assert {status["vault_id"] for status in statuses} == {layout.identity.vault_id}
    assert {status["state"] for status in statuses} == {"ready"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        reads = [
            executor.submit(item.call, "tool:list_recent_notes", {"days": 7}) for item in clients
        ]
        note.unlink()
        assert all(future.result() for future in reads)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "No notes modified" in client.call("tool:list_recent_notes", {"days": 7}):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("watcher update was not indexed")
    client.call("shutdown")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not layout.endpoint_path.exists()
