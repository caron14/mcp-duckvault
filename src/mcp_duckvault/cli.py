"""DuckVault command-line interface."""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional

import click
import duckdb

from .daemon import DaemonClient, DuckVaultDaemon, ensure_daemon
from .db_manager import DatabaseManager, safe_database_permissions
from .errors import DuckVaultError
from .graph_repository import GraphRepository
from .graph_visualizer import write_graph_visualization
from .indexer import EmbeddingModel, VaultIndexer
from .mcp_proxy import create_proxy_server
from .vault_identity import VaultLayout, duckvault_home, model_cache_path

logger = logging.getLogger("mcp_duckvault")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _emit(payload: dict[str, object], json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, default=str))
        return
    for key, value in payload.items():
        if value is not None:
            click.echo(f"{key}: {value}")


def _fail(exc: Exception, json_output: bool = False) -> None:
    if isinstance(exc, DuckVaultError):
        payload = exc.as_dict()
    else:
        payload = {"code": type(exc).__name__, "message": str(exc), "retryable": False}
    if json_output:
        click.echo(json.dumps({"status": "failed", "error": payload}), err=False)
    else:
        click.echo(f"Error [{payload['code']}]: {payload['message']}", err=True)
    raise click.exceptions.Exit(1)


def _db_path(layout: VaultLayout, custom: str | None) -> str:
    return custom or str(layout.db_path)


def _initialize_vault(
    vault_path: str,
    *,
    custom_db_path: str | None = None,
) -> dict[str, object]:
    layout = VaultLayout.for_vault(vault_path, create=True)
    selected_db = _db_path(layout, custom_db_path)
    model_cache = model_cache_path(create=True)
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(model_cache)

    DatabaseManager.prepare_vss(selected_db)
    model = EmbeddingModel()
    # Explicit initialization is the only path allowed to download the model.
    model.model
    os.environ["HF_HUB_OFFLINE"] = "1"
    offline_model = EmbeddingModel(model.model_name, allow_download=False)
    offline_model.model

    db = DatabaseManager(selected_db, identity=layout.identity)
    try:
        db.initialize_schema(offline_model.dimension, model_id=offline_model.model_name)
        summary = VaultIndexer(
            str(layout.identity.normalized_path), db, model=offline_model
        ).full_sync()
        # A query embedding plus a DB read proves the offline search path is operational,
        # including for an empty Vault.
        offline_model.encode(["duckvault initialization smoke test"], is_query=True)
        db.conn.execute("SELECT count(*) FROM chunks").fetchone()
    finally:
        db.close()
    safe_database_permissions(selected_db)

    generated = {
        "mcpServers": {
            "duckvault": {
                "command": "duckvault",
                "args": ["serve", str(layout.identity.normalized_path)],
            }
        }
    }
    layout.generated_mcp_config_path.write_text(
        json.dumps(generated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    try:
        layout.generated_mcp_config_path.chmod(0o600)
    except OSError:
        pass
    return {
        "status": summary.status,
        "vault_id": layout.identity.vault_id,
        "vault_path": str(layout.identity.normalized_path),
        "database": selected_db,
        "model_cache": str(model_cache),
        "mcp_config": str(layout.generated_mcp_config_path),
        "sync": summary.as_dict(),
        "offline_ready": True,
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging on stderr.")
@click.version_option(version=version("mcp-duckvault"), prog_name="duckvault")
def main(verbose: bool) -> None:
    """Initialize, inspect, and serve local Markdown Vault indexes."""
    setup_logging(verbose)


@main.command("init")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option("--non-interactive", is_flag=True, help="Fail instead of prompting for decisions.")
@click.option("--json", "json_output", is_flag=True, help="Emit one JSON result on stdout.")
def init_command(
    vault_path: Path, db_path: str | None, non_interactive: bool, json_output: bool
) -> None:
    """Prepare VSS/model, create the DB, sync, and generate MCP configuration."""
    del non_interactive  # Initialization is deterministic; destructive choices are separate commands.
    try:
        result = _initialize_vault(str(vault_path), custom_db_path=db_path)
        _emit(result, json_output)
        if result["status"] == "partial":
            raise click.exceptions.Exit(2)
        if result["status"] == "failed":
            raise click.exceptions.Exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        _fail(exc, json_output)


@main.command("serve")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
def serve_command(vault_path: Path, db_path: str | None) -> None:
    """Run a lightweight stdio MCP proxy for a Vault."""
    try:
        layout = VaultLayout.for_vault(vault_path)
        client = ensure_daemon(layout, db_path=db_path)
        create_proxy_server(client).run()
    except Exception as exc:
        _fail(exc)


@main.command("sync")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option("--json", "json_output", is_flag=True)
def sync_command(vault_path: Path, db_path: str | None, json_output: bool) -> None:
    """Synchronize a Vault through its single-writer daemon."""
    try:
        layout = VaultLayout.for_vault(vault_path)
        result = ensure_daemon(layout, db_path=db_path).call("sync", request_timeout=3600.0)
        _emit(result, json_output)
        if result["status"] == "partial":
            raise click.exceptions.Exit(2)
        if result["status"] == "failed":
            raise click.exceptions.Exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        _fail(exc, json_output)


def _offline_status(layout: VaultLayout, selected_db: str) -> dict[str, object]:
    if not Path(selected_db).exists():
        raise DuckVaultError("NOT_INITIALIZED", "Vault has not been initialized.")
    db = DatabaseManager(selected_db, identity=layout.identity, read_only=True)
    try:
        db.connect(load_vss=False)
        db.verify_vault_identity()
        return {"state": "stopped", **db.status()}
    finally:
        db.close()


@main.command("status")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option("--json", "json_output", is_flag=True)
def status_command(vault_path: Path, db_path: str | None, json_output: bool) -> None:
    """Show daemon readiness and the latest synchronization result."""
    try:
        layout = VaultLayout.for_vault(vault_path)
        try:
            result = DaemonClient(layout, timeout=1).call("status")
        except DuckVaultError:
            result = _offline_status(layout, _db_path(layout, db_path))
        _emit(result, json_output)
    except Exception as exc:
        _fail(exc, json_output)


def _doctor_checks(layout: VaultLayout, selected_db: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    daemon_status_result: dict[str, object] | None = None
    database_status: dict[str, object] | None = None

    def add(code: str, ok: bool, detail: str, repair: str | None = None) -> None:
        checks.append({"code": code, "ok": ok, "detail": detail, "repair": repair})

    add("PYTHON_VERSION", sys.version_info >= (3, 11), sys.version.split()[0])
    add(
        "VAULT_READABLE",
        os.access(layout.identity.normalized_path, os.R_OK),
        str(layout.identity.normalized_path),
    )
    add(
        "VAULT_WATCHABLE",
        os.access(layout.identity.normalized_path, os.R_OK | os.X_OK),
        "Vault permissions",
    )
    try:
        conn = duckdb.connect(":memory:")
        conn.execute("LOAD vss")
        conn.close()
        add("VSS_OFFLINE", True, "VSS extension loads without INSTALL")
    except Exception as exc:
        add(
            "VSS_OFFLINE",
            False,
            type(exc).__name__,
            f"duckvault init {layout.identity.normalized_path}",
        )

    cache = model_cache_path()
    previous_offline = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(cache)
    try:
        EmbeddingModel(allow_download=False).model
        add("MODEL_OFFLINE", True, str(cache))
    except Exception as exc:
        add(
            "MODEL_OFFLINE",
            False,
            type(exc).__name__,
            f"duckvault init {layout.identity.normalized_path}",
        )
    finally:
        if previous_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous_offline

    try:
        daemon_client = DaemonClient(layout, timeout=1)
        health = daemon_client.health()
        add(
            "DAEMON",
            health.get("state") not in {"failed"},
            str(health.get("state")),
            f"duckvault daemon restart {layout.identity.normalized_path}",
        )
        add("WATCHER", health.get("state") not in {"failed", "starting"}, "owned by daemon")
        if health.get("state") not in {"starting", "preparing", "failed"}:
            daemon_status_result = daemon_client.call("status")
    except DuckVaultError:
        add("DAEMON", True, "stopped", f"duckvault daemon start {layout.identity.normalized_path}")
        add("WATCHER", True, "stopped with daemon")

    if daemon_status_result is not None:
        database_status = daemon_status_result
        actual_db = daemon_status_result.get("db_path")
        database_matches = (
            actual_db is None or Path(str(actual_db)) == Path(selected_db).expanduser().resolve()
        )
        identity_matches = daemon_status_result.get(
            "vault_id"
        ) == layout.identity.vault_id and daemon_status_result.get("vault_path") == str(
            layout.identity.normalized_path
        )
        add(
            "DATABASE",
            database_matches,
            str(actual_db or selected_db),
            f"duckvault daemon restart {layout.identity.normalized_path}",
        )
        add(
            "VAULT_IDENTITY",
            identity_matches,
            str(daemon_status_result.get("vault_id")),
            f"duckvault daemon restart {layout.identity.normalized_path}",
        )
    elif not Path(selected_db).exists():
        add(
            "DATABASE",
            False,
            "not initialized",
            f"duckvault init {layout.identity.normalized_path}",
        )
        add(
            "VAULT_IDENTITY",
            False,
            "database unavailable",
            f"duckvault init {layout.identity.normalized_path}",
        )
    else:
        try:
            db = DatabaseManager(selected_db, identity=layout.identity, read_only=True)
            db.connect(load_vss=False)
            db.verify_vault_identity()
            db.conn.execute("SELECT 1").fetchone()
            database_status = db.status(failure_limit=0)
            db.close()
            add("DATABASE", True, selected_db)
            add("VAULT_IDENTITY", True, layout.identity.vault_id)
        except Exception as exc:
            add(
                "DATABASE",
                False,
                type(exc).__name__,
                f"duckvault migrate-legacy {layout.identity.normalized_path}",
            )
            add(
                "VAULT_IDENTITY",
                False,
                type(exc).__name__,
                f"duckvault migrate-legacy {layout.identity.normalized_path}",
            )
    if database_status is not None:
        reindex = database_status.get("reindex") or {}
        required = bool(reindex.get("required"))
        add(
            "REINDEX_READY",
            not required,
            str(database_status.get("index_state", "unknown")),
            str(reindex.get("repair")) if required else None,
        )
    return checks


@main.command("doctor")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option("--json", "json_output", is_flag=True)
def doctor_command(vault_path: Path, db_path: str | None, json_output: bool) -> None:
    """Diagnose dependencies, identity, permissions, daemon, and offline readiness."""
    try:
        layout = VaultLayout.for_vault(vault_path)
        checks = _doctor_checks(layout, _db_path(layout, db_path))
        payload = {
            "status": "ok" if all(check["ok"] for check in checks) else "failed",
            "checks": checks,
        }
        if json_output:
            click.echo(json.dumps(payload, ensure_ascii=False))
        else:
            for check in checks:
                marker = "OK" if check["ok"] else "FAIL"
                click.echo(f"[{marker}] {check['code']}: {check['detail']}")
                if not check["ok"] and check["repair"]:
                    click.echo(f"  Repair: {check['repair']}")
        if payload["status"] == "failed":
            raise click.exceptions.Exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        _fail(exc, json_output)


@main.command("migrate-legacy")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--legacy-db",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=lambda: duckvault_home() / "vault.db",
    show_default="~/.duckvault/vault.db",
)
@click.option("--json", "json_output", is_flag=True)
def migrate_legacy_command(vault_path: Path, legacy_db: Path, json_output: bool) -> None:
    """Back up a legacy global DB and rebuild a Vault-specific index."""
    try:
        layout = VaultLayout.for_vault(vault_path, create=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = layout.backups_dir / f"legacy-vault-{stamp}.db"
        DatabaseManager(str(legacy_db)).backup(backup)
        result = _initialize_vault(str(vault_path))
        result["legacy_backup"] = str(backup)
        result["legacy_source_preserved"] = str(legacy_db)
        _emit(result, json_output)
    except Exception as exc:
        _fail(exc, json_output)


def _ensure_daemon_stopped(layout: VaultLayout) -> None:
    try:
        DaemonClient(layout, timeout=0.5).health()
    except DuckVaultError as exc:
        if exc.code in {"DAEMON_NOT_RUNNING", "DAEMON_UNREACHABLE"}:
            return
        raise
    raise DuckVaultError(
        "DAEMON_RUNNING",
        f"Stop the daemon before rebuilding: duckvault daemon stop "
        f"{layout.identity.normalized_path}",
    )


def _reindex_vault(
    vault_path: str,
    *,
    custom_db_path: str | None = None,
    model: EmbeddingModel | None = None,
    load_vss: bool = True,
) -> dict[str, object]:
    """Build a replacement database and atomically install it only on success."""
    layout = VaultLayout.for_vault(vault_path, create=True)
    _ensure_daemon_stopped(layout)
    selected_db = Path(_db_path(layout, custom_db_path)).expanduser().resolve()
    if not selected_db.exists():
        raise DuckVaultError("NOT_INITIALIZED", "Vault has not been initialized.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = layout.backups_dir / f"reindex-{stamp}.db"
    original = DatabaseManager(str(selected_db), identity=layout.identity)
    original.connect(load_vss=False)
    original.verify_vault_identity()
    original.backup(backup)

    replacement_path = selected_db.with_name(f".{selected_db.name}.reindex-{uuid.uuid4().hex}.tmp")
    replacement: DatabaseManager | None = None
    try:
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(model_cache_path())
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        offline_model = model or EmbeddingModel(allow_download=False)
        replacement = DatabaseManager(str(replacement_path), identity=layout.identity)
        replacement.connect(load_vss=load_vss)
        replacement.initialize_schema(
            embedding_dim=offline_model.dimension,
            model_id=offline_model.model_name,
        )
        summary = VaultIndexer(
            str(layout.identity.normalized_path), replacement, model=offline_model
        ).full_sync()
        if summary.status != "complete":
            raise DuckVaultError(
                "REINDEX_PARTIAL",
                f"Replacement index has {summary.failed} failed file(s).",
                retryable=True,
            )
        replacement.set_config("index_state", "ready")
        replacement.set_config("last_reindex_backup", backup)
        replacement.delete_config("last_reindex_error")
        replacement.conn.execute("CHECKPOINT")
        replacement.close()
        replacement = None
        os.replace(replacement_path, selected_db)
        safe_database_permissions(selected_db)
        return {
            "status": "complete",
            "database": str(selected_db),
            "backup": str(backup),
            "sync": summary.as_dict(),
        }
    except Exception as exc:
        if replacement is not None:
            replacement.close()
        recovery = DatabaseManager(str(selected_db), identity=layout.identity)
        try:
            recovery.connect(load_vss=False)
            recovery.set_config("index_state", "reindex_failed")
            recovery.set_config("last_reindex_error", type(exc).__name__)
            recovery.set_config("last_reindex_backup", backup)
        finally:
            recovery.close()
        if isinstance(exc, DuckVaultError):
            raise
        raise DuckVaultError(
            "REINDEX_FAILED",
            f"Reindex failed; the original database is preserved and its backup is {backup}.",
            retryable=True,
        ) from exc
    finally:
        for temporary in (replacement_path, replacement_path.with_suffix(".tmp.wal")):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@main.command("reindex")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option("--json", "json_output", is_flag=True)
def reindex_command(vault_path: Path, db_path: str | None, json_output: bool) -> None:
    """Safely rebuild a Vault index while retaining the previous database."""
    try:
        _emit(
            _reindex_vault(str(vault_path), custom_db_path=db_path),
            json_output,
        )
    except Exception as exc:
        _fail(exc, json_output)


@main.command("visualize")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False), help="Advanced custom DB path.")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("duckvault-graph.html"),
    show_default=True,
)
@click.option("--json-output", type=click.Path(dir_okay=False, path_type=Path))
def visualize_command(
    vault_path: Path, db_path: str | None, output: Path, json_output: Optional[Path]
) -> None:
    """Export an offline graph viewer from an initialized, stopped Vault."""
    try:
        layout = VaultLayout.for_vault(vault_path)
        try:
            DaemonClient(layout, timeout=0.5).health()
            raise DuckVaultError("DAEMON_RUNNING", "Stop the daemon before exporting a graph.")
        except DuckVaultError as exc:
            if exc.code != "DAEMON_NOT_RUNNING" and exc.code != "DAEMON_UNREACHABLE":
                raise
        db = DatabaseManager(_db_path(layout, db_path), identity=layout.identity, read_only=True)
        db.connect(load_vss=False)
        db.verify_vault_identity()
        html_path, graph_path = write_graph_visualization(
            GraphRepository(db), str(layout.identity.normalized_path), output, json_output
        )
        db.close()
        _emit({"html": str(html_path), "json": str(graph_path)}, False)
    except Exception as exc:
        _fail(exc)


@main.group("daemon")
def daemon_group() -> None:
    """Manage the per-Vault shared daemon."""


@daemon_group.command("start")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def daemon_start(vault_path: Path) -> None:
    try:
        health = ensure_daemon(VaultLayout.for_vault(vault_path)).health()
        _emit(health, False)
    except Exception as exc:
        _fail(exc)


@daemon_group.command("status")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def daemon_status(vault_path: Path) -> None:
    try:
        _emit(DaemonClient(VaultLayout.for_vault(vault_path), timeout=1).health(), False)
    except Exception as exc:
        _fail(exc)


@daemon_group.command("stop")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def daemon_stop(vault_path: Path) -> None:
    try:
        _emit(DaemonClient(VaultLayout.for_vault(vault_path), timeout=2).call("shutdown"), False)
    except Exception as exc:
        _fail(exc)


@daemon_group.command("restart")
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def daemon_restart(vault_path: Path) -> None:
    layout = VaultLayout.for_vault(vault_path)
    try:
        try:
            DaemonClient(layout, timeout=2).call("shutdown")
            deadline = datetime.now().timestamp() + 10
            while layout.endpoint_path.exists() and datetime.now().timestamp() < deadline:
                import time

                time.sleep(0.05)
        except DuckVaultError:
            pass
        _emit(ensure_daemon(layout).health(), False)
    except Exception as exc:
        _fail(exc)


@main.command("_daemon-run", hidden=True)
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db-path", type=click.Path(dir_okay=False))
def daemon_run(vault_path: Path, db_path: str | None) -> None:
    """Internal detached daemon entrypoint."""
    layout = VaultLayout.for_vault(vault_path, create=True)
    DuckVaultDaemon(layout, db_path=db_path).run()


if __name__ == "__main__":
    main()
