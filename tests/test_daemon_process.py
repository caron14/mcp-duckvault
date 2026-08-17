"""POSIX process-boundary daemon recovery tests."""

import asyncio
import concurrent.futures
import multiprocessing
import os
import signal
import time

import pytest

from mcp_duckvault.daemon import DaemonClient, ensure_daemon
from mcp_duckvault.indexer import VaultIndexer
from mcp_duckvault.mcp_proxy import create_proxy_server
from mcp_duckvault.vault_identity import VaultLayout

PROCESS_START_TIMEOUT = 25


class ProcessEmbeddingModel:
    model_name = "test/process-model"
    dimension = 384

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def encode(self, texts, is_query=False):
        del is_query
        delay = float(os.environ.get("DUCKVAULT_TEST_MODEL_DELAY", "0"))
        if delay:
            time.sleep(delay)
        return [[1.0] + [0.0] * 383 for _ in texts]


def _run_daemon_process(vault_path, home, outcomes):
    os.environ["DUCKVAULT_HOME"] = home
    os.environ["HF_HUB_OFFLINE"] = "1"
    import mcp_duckvault.daemon as daemon_module

    daemon_module.EmbeddingModel = ProcessEmbeddingModel
    layout = VaultLayout.for_vault(vault_path, create=True)
    try:
        daemon_module.DuckVaultDaemon(layout, load_vss=False).run()
    except Exception as exc:
        outcomes.put(getattr(exc, "code", type(exc).__name__))
    else:
        outcomes.put("stopped")


def _start_process(context, vault, home):
    outcomes = context.Queue()
    process = context.Process(
        target=_run_daemon_process,
        args=(str(vault), str(home), outcomes),
    )
    try:
        process.start()
    except Exception:
        outcomes.close()
        outcomes.join_thread()
        raise
    return process, outcomes


def _cleanup_processes(resources):
    """Stop spawned daemons and release multiprocessing IPC resources."""
    for process, outcomes in reversed(resources):
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            if not process.is_alive():
                process.close()
        finally:
            outcomes.close()
            outcomes.join_thread()


def _wait_ready(layout, timeout=PROCESS_START_TIMEOUT):
    client = DaemonClient(layout, timeout=2)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = client.health()
            if health["state"] in {"ready", "degraded"}:
                return client
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError("daemon did not become ready")


def _wait_recent_paths(client, expected, absent=(), timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.call("tool:list_recent_notes", {"days": 7, "limit": 100})
        paths = {item["path"] for item in result["items"]}
        if set(expected) <= paths and not (set(absent) & paths):
            return
        time.sleep(0.05)
    raise AssertionError(f"recent paths did not converge: expected={expected}, absent={absent}")


def _initialize_process_database(vault, layout, database_factory):
    db = database_factory(
        str(layout.db_path),
        identity=layout.identity,
        embedding_dim=384,
        model_id=ProcessEmbeddingModel.model_name,
    )
    VaultIndexer(str(vault), db, model=ProcessEmbeddingModel()).full_sync()
    db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals are required")
def test_process_owner_three_proxies_watcher_and_sigterm_drain(
    tmp_path, monkeypatch, database_factory
):
    context = multiprocessing.get_context("spawn")
    resources = []
    home = tmp_path / "home"
    monkeypatch.setenv("DUCKVAULT_HOME", str(home))
    monkeypatch.setenv("DUCKVAULT_TEST_MODEL_DELAY", "0.15")
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Initial", encoding="utf-8")
    layout = VaultLayout.for_vault(vault, create=True)
    _initialize_process_database(vault, layout, database_factory)

    try:
        owner, owner_outcomes = _start_process(context, vault, home)
        resources.append((owner, owner_outcomes))
        client = _wait_ready(layout)
        contender, contender_outcomes = _start_process(context, vault, home)
        resources.append((contender, contender_outcomes))
        assert contender_outcomes.get(timeout=PROCESS_START_TIMEOUT) == "DAEMON_ALREADY_RUNNING"
        contender.join(timeout=5)
        assert not contender.is_alive()

        proxies = [create_proxy_server(DaemonClient(layout, timeout=5)) for _ in range(3)]

        def recent(proxy):
            async def invoke():
                return await proxy._tool_manager.call_tool(
                    "list_recent_notes", {"days": 7}, convert_result=False
                )

            return asyncio.run(invoke())

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(recent, proxies))
        assert all(result["items"][0]["path"] == "note.md" for result in results)

        created = vault / "created.md"
        moved = vault / "moved.md"
        created.write_text("# Created", encoding="utf-8")
        _wait_recent_paths(client, {"created.md", "note.md"})
        created.rename(moved)
        _wait_recent_paths(client, {"moved.md", "note.md"}, {"created.md"})
        moved.unlink()
        _wait_recent_paths(client, {"note.md"}, {"created.md", "moved.md"})

        note.write_text("# Updated during concurrent work", encoding="utf-8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            sync = executor.submit(client.call, "sync", {}, request_timeout=10)
            searches = [
                executor.submit(
                    client.call, "tool:search_notes", {"query": "updated"}, request_timeout=10
                )
                for _ in range(3)
            ]
            assert sync.result(timeout=10)["status"] == "complete"
            assert all(search.result(timeout=10)["count"] >= 1 for search in searches)

        note.write_text("# Updated before graceful stop", encoding="utf-8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            sync_future = executor.submit(client.call, "sync", {}, request_timeout=10)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and client.health()["state"] != "syncing":
                time.sleep(0.02)
            os.kill(owner.pid, signal.SIGTERM)
            assert sync_future.result(timeout=10)["status"] == "complete"

        owner.join(timeout=10)
        assert not owner.is_alive()
        assert owner_outcomes.get(timeout=2) == "stopped"
        assert not layout.endpoint_path.exists()

        from mcp_duckvault.db_manager import DatabaseManager

        recovered = DatabaseManager(str(layout.db_path), identity=layout.identity, read_only=True)
        recovered.connect(load_vss=False)
        assert (
            "Updated before graceful stop"
            in recovered.conn.execute(
                "SELECT content FROM chunks WHERE document_path = 'note.md'"
            ).fetchone()[0]
        )
        recovered.close()
    finally:
        _cleanup_processes(resources)


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is required")
def test_sigkill_stale_endpoint_recovers_without_database_loss(
    tmp_path, monkeypatch, database_factory
):
    context = multiprocessing.get_context("spawn")
    resources = []
    home = tmp_path / "home"
    monkeypatch.setenv("DUCKVAULT_HOME", str(home))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Durable", encoding="utf-8")
    layout = VaultLayout.for_vault(vault, create=True)
    _initialize_process_database(vault, layout, database_factory)
    try:
        owner, owner_outcomes = _start_process(context, vault, home)
        resources.append((owner, owner_outcomes))
        client = _wait_ready(layout)
        pid = client.health()["pid"]

        os.kill(pid, signal.SIGKILL)
        owner.join(timeout=5)
        assert not owner.is_alive()
        assert layout.endpoint_path.exists()
        assert layout.owner_lock_path.exists()

        started = []

        def launch_replacement(*_args, **_kwargs):
            process, outcomes = _start_process(context, vault, home)
            resources.append((process, outcomes))
            started.append((process, outcomes))
            return process

        import mcp_duckvault.daemon as daemon_module

        monkeypatch.setattr(daemon_module.subprocess, "Popen", launch_replacement)
        replacement_client = ensure_daemon(layout, timeout=PROCESS_START_TIMEOUT)
        assert replacement_client.health()["pid"] != pid
        result = replacement_client.call("tool:search_notes", {"query": "durable"})
        assert result["items"][0]["path"] == "note.md"

        replacement_client.call("shutdown")
        replacement, outcomes = started[0]
        replacement.join(timeout=10)
        assert not replacement.is_alive()
        assert outcomes.get(timeout=2) == "stopped"
    finally:
        _cleanup_processes(resources)
