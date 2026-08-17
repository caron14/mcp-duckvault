"""Per-Vault single-writer daemon and authenticated local RPC client."""

import asyncio
import json
import logging
import os
import queue
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .db_manager import DatabaseManager
from .errors import DuckVaultError
from .indexer import EmbeddingModel, VaultIndexer
from .mcp_server import create_mcp_server
from .vault_identity import VaultLayout, model_cache_path

logger = logging.getLogger(__name__)
RPC_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def read_endpoint(layout: VaultLayout) -> dict[str, Any] | None:
    try:
        data = json.loads(layout.endpoint_path.read_text(encoding="utf-8"))
        if data.get("protocol_version") != RPC_PROTOCOL_VERSION:
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


class DaemonClient:
    def __init__(self, layout: VaultLayout, *, timeout: float = 10.0):
        self.layout = layout
        self.timeout = timeout

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> Any:
        timeout = self.timeout if request_timeout is None else request_timeout
        endpoint = read_endpoint(self.layout)
        if endpoint is None:
            raise DuckVaultError(
                "DAEMON_NOT_RUNNING", "DuckVault daemon is not running.", retryable=True
            )
        request = (
            json.dumps(
                {
                    "id": secrets.token_hex(8),
                    "token": endpoint["token"],
                    "method": method,
                    "params": params or {},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with socket.create_connection(
                (str(endpoint["host"]), int(endpoint["port"])), timeout
            ) as connection:
                connection.settimeout(timeout)
                connection.sendall(request)
                response_file = connection.makefile("rb")
                response_line = response_file.readline(MAX_RESPONSE_BYTES + 1)
        except (OSError, KeyError, ValueError) as exc:
            raise DuckVaultError(
                "DAEMON_UNREACHABLE",
                f"DuckVault daemon endpoint is unreachable ({type(exc).__name__}).",
                retryable=True,
            ) from exc
        if not response_line or len(response_line) > MAX_RESPONSE_BYTES:
            raise DuckVaultError(
                "DAEMON_PROTOCOL_ERROR", "Invalid daemon response.", retryable=True
            )
        response = json.loads(response_line)
        if "error" in response:
            error = response["error"]
            raise DuckVaultError(
                str(error.get("code", "DAEMON_ERROR")),
                str(error.get("message", "Daemon request failed.")),
                retryable=bool(error.get("retryable", False)),
            )
        return response.get("result")

    def health(self) -> dict[str, object]:
        return self.call("health", request_timeout=min(self.timeout, 1.0))


class _QueueWatchdogHandler(FileSystemEventHandler):
    def __init__(self, worker: "DaemonWorker"):
        self.worker = worker

    def on_modified(self, event: Any) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self.worker.enqueue("index_file", {"path": event.src_path})

    on_created = on_modified

    def on_deleted(self, event: Any) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self.worker.enqueue("delete_file", {"path": event.src_path})

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".md"):
            self.worker.enqueue("delete_file", {"path": event.src_path})
        if event.dest_path.endswith(".md"):
            self.worker.enqueue("index_file", {"path": event.dest_path})


class DaemonWorker:
    """Single thread that owns DuckDB, VSS, model, watcher, and all operations."""

    def __init__(self, layout: VaultLayout, db_path: str, *, load_vss: bool = True):
        self.layout = layout
        self.db_path = db_path
        self.load_vss = load_vss
        self.jobs: queue.Queue[tuple[str, dict[str, object], Future[Any] | None] | None] = (
            queue.Queue()
        )
        self.thread = threading.Thread(target=self._run, name="duckvault-db-owner", daemon=True)
        self.ready = threading.Event()
        self._state_lock = threading.Lock()
        self._state = "starting"
        self._error: dict[str, object] | None = None
        self._index_status: dict[str, object] = {
            "index_state": "unknown",
            "last_sync": None,
            "failures": [],
        }
        self._observer: Observer | None = None
        self._db: DatabaseManager | None = None
        self._pending_paths: set[tuple[str, str]] = set()
        self._pending_lock = threading.Lock()

    def set_state(self, state: str, error: dict[str, object] | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._error = error

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return {"state": self._state, "error": self._error, "queue_depth": self.jobs.qsize()}

    def status_snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "state": self._state,
                "error": self._error,
                "queue_depth": self.jobs.qsize(),
                **self._index_status,
            }

    def set_index_status(self, status: dict[str, object]) -> None:
        with self._state_lock:
            self._index_status = status

    def start(self) -> None:
        self.thread.start()

    def enqueue(self, operation: str, params: dict[str, object]) -> None:
        if operation in {"index_file", "delete_file"}:
            key = (operation, str(params["path"]))
            with self._pending_lock:
                if key in self._pending_paths:
                    return
                self._pending_paths.add(key)
        self.jobs.put((operation, params, None))

    def submit(self, operation: str, params: dict[str, object], timeout: float = 300.0) -> Any:
        state = self.snapshot()["state"]
        if state in {"starting", "preparing"}:
            raise DuckVaultError(
                "DAEMON_NOT_READY", "DuckVault daemon is preparing.", retryable=True
            )
        if state in {"reindex_required", "reindex_failed"} and (
            operation == "sync" or operation.startswith("tool:")
        ):
            raise DuckVaultError(
                "REINDEX_REQUIRED",
                f"The Vault index must be rebuilt with 'duckvault reindex "
                f"{self.layout.identity.normalized_path}'.",
                retryable=True,
            )
        if state == "failed":
            raise DuckVaultError("DAEMON_FAILED", "DuckVault daemon initialization failed.")
        future: Future[Any] = Future()
        self.jobs.put((operation, params, future))
        return future.result(timeout=timeout)

    def stop(self, timeout: float = 15.0) -> None:
        self.set_state("stopping")
        self.jobs.put(None)
        self.thread.join(timeout=timeout)

    def _initialize(self) -> tuple[DatabaseManager, VaultIndexer, Any]:
        self.set_state("preparing")
        if self.db_path != ":memory:" and not Path(self.db_path).exists():
            raise DuckVaultError(
                "NOT_INITIALIZED",
                f"Vault is not initialized. Run 'duckvault init {self.layout.identity.normalized_path}'.",
            )
        os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(model_cache_path())
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        model = EmbeddingModel(allow_download=False)
        db = DatabaseManager(self.db_path, identity=self.layout.identity)
        db.connect(load_vss=self.load_vss)
        db.initialize_schema(embedding_dim=model.dimension, model_id=model.model_name)
        self.set_index_status(db.status())
        if db._config("index_state") in {"reindex_required", "reindex_failed"}:
            self.set_state("reindex_required")
        else:
            self.set_state("ready")
        indexer = VaultIndexer(str(self.layout.identity.normalized_path), db, model=model)
        mcp = create_mcp_server(
            str(self.layout.identity.normalized_path), db, model, manage_connection=False
        )
        handler = _QueueWatchdogHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.layout.identity.normalized_path), recursive=True)
        self._observer.start()
        return db, indexer, mcp

    def _run(self) -> None:
        try:
            db, indexer, mcp = self._initialize()
            self._db = db
            self.ready.set()
            # Reconcile changes made while the daemon was stopped without delaying
            # endpoint publication or MCP capability discovery.
            if self.snapshot()["state"] != "reindex_required":
                self.jobs.put(("sync", {}, None))
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                operation, params, future = job
                if operation in {"index_file", "delete_file"}:
                    with self._pending_lock:
                        self._pending_paths.discard((operation, str(params["path"])))
                try:
                    if operation == "sync":
                        self.set_state("syncing")
                        result = indexer.full_sync().as_dict()
                        self.set_index_status(db.status())
                        self.set_state("ready" if result["status"] == "complete" else "degraded")
                    elif operation == "status":
                        result = self.status_snapshot()
                    elif operation == "index_file":
                        result = indexer.index_file(str(params["path"])).as_dict()
                        if result["status"] == "failed":
                            db.set_config("index_state", "degraded")
                            self.set_state("degraded")
                        elif not db.status(failure_limit=1)["failures"]:
                            db.set_config("index_state", "ready")
                            self.set_state("ready")
                        self.set_index_status(db.status())
                    elif operation == "delete_file":
                        indexer.delete_file(str(params["path"]))
                        if not db.status(failure_limit=1)["failures"]:
                            db.set_config("index_state", "ready")
                            self.set_state("ready")
                        self.set_index_status(db.status())
                        result = {"status": "deleted"}
                    elif operation.startswith("tool:"):
                        result = asyncio.run(
                            mcp._tool_manager.call_tool(
                                operation.removeprefix("tool:"), params, convert_result=False
                            )
                        )
                    else:
                        raise DuckVaultError("UNKNOWN_OPERATION", f"Unknown operation: {operation}")
                    if future:
                        future.set_result(result)
                except Exception as exc:
                    logger.error("Daemon operation %s failed (%s)", operation, type(exc).__name__)
                    if operation == "sync":
                        self.set_state("degraded")
                    if future:
                        future.set_exception(exc)
        except Exception as exc:
            logger.exception("Daemon worker initialization failed")
            error = (
                exc.as_dict()
                if isinstance(exc, DuckVaultError)
                else {
                    "code": "DAEMON_INIT_FAILED",
                    "message": type(exc).__name__,
                    "retryable": False,
                }
            )
            self.set_state("failed", error)
            self.ready.set()
        finally:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=5)
            if self._db:
                try:
                    self._db.conn.execute("CHECKPOINT")
                except Exception:
                    pass
                self._db.close()


class _RpcHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        daemon: "DuckVaultDaemon" = self.server.daemon_controller  # type: ignore[attr-defined]
        request_id: object = None
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line or len(line) > MAX_REQUEST_BYTES:
                raise DuckVaultError("REQUEST_TOO_LARGE", "Daemon request is too large.")
            request = json.loads(line)
            request_id = request.get("id")
            if not secrets.compare_digest(str(request.get("token", "")), daemon.token):
                raise DuckVaultError("UNAUTHORIZED", "Invalid daemon token.")
            method = str(request.get("method", ""))
            params = request.get("params") or {}
            result = daemon.dispatch(method, params)
            response = {"id": request_id, "result": result}
        except Exception as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, DuckVaultError)
                else {"code": "DAEMON_ERROR", "message": type(exc).__name__, "retryable": False}
            )
            response = {"id": request_id, "error": error}
        encoded = json.dumps(response, default=str, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": "RESPONSE_TOO_LARGE",
                            "message": "Daemon response exceeds the configured limit.",
                            "retryable": False,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        self.wfile.write(encoded)


class _RpcServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class DuckVaultDaemon:
    def __init__(
        self,
        layout: VaultLayout,
        db_path: str | None = None,
        *,
        load_vss: bool = True,
    ):
        self.layout = layout
        self.db_path = db_path or str(layout.db_path)
        self.token = secrets.token_urlsafe(32)
        self.worker = DaemonWorker(layout, self.db_path, load_vss=load_vss)
        self.server = _RpcServer(("127.0.0.1", 0), _RpcHandler)
        self.server.daemon_controller = self  # type: ignore[attr-defined]
        self._shutdown_started = False

    def dispatch(self, method: str, params: dict[str, object]) -> Any:
        if method == "health":
            return {
                **self.worker.snapshot(),
                "pid": os.getpid(),
                "vault_id": self.layout.identity.vault_id,
                "db_path": str(Path(self.db_path).expanduser().resolve()),
                "protocol_version": RPC_PROTOCOL_VERSION,
            }
        if method == "shutdown":
            self.request_shutdown()
            return {"status": "stopping"}
        if method == "status":
            status = self.worker.status_snapshot()
            failure_limit = max(0, int(params.get("failure_limit", 100)))
            status["failures"] = status.get("failures", [])[:failure_limit]
            status["db_path"] = str(Path(self.db_path).expanduser().resolve())
            status.setdefault("vault_id", self.layout.identity.vault_id)
            status.setdefault("vault_path", str(self.layout.identity.normalized_path))
            return status
        if method == "sync":
            return self.worker.submit("sync", params)
        if method.startswith("tool:"):
            return self.worker.submit(method, params)
        raise DuckVaultError("UNKNOWN_METHOD", f"Unknown daemon method: {method}")

    def request_shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def run(self) -> None:
        self.layout.ensure()
        owner_lock = FileLock(str(self.layout.owner_lock_path))
        try:
            owner_lock.acquire(timeout=0)
        except Timeout as exc:
            raise DuckVaultError(
                "DAEMON_ALREADY_RUNNING", "A daemon already owns this Vault."
            ) from exc
        try:
            self.layout.owner_lock_path.chmod(0o600)
        except OSError:
            pass
        host, port = self.server.server_address
        endpoint = {
            "protocol_version": RPC_PROTOCOL_VERSION,
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "token": self.token,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "vault_id": self.layout.identity.vault_id,
            "db_path": str(Path(self.db_path).expanduser().resolve()),
        }
        _atomic_json(self.layout.endpoint_path, endpoint)
        self.worker.start()

        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_args: self.request_shutdown())
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.worker.stop()
            self.server.server_close()
            try:
                self.layout.endpoint_path.unlink()
            except FileNotFoundError:
                pass
            owner_lock.release()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def ensure_daemon(
    layout: VaultLayout, *, db_path: str | None = None, timeout: float = 8.0
) -> DaemonClient:
    client = DaemonClient(layout, timeout=300.0)
    selected_db = Path(db_path).expanduser().resolve() if db_path else layout.db_path.resolve()

    def checked_health() -> dict[str, object]:
        health = client.health()
        if health.get("state") == "failed":
            error = health.get("error") or {}
            raise DuckVaultError(
                str(error.get("code", "DAEMON_FAILED")),
                str(error.get("message", "DuckVault daemon initialization failed.")),
                retryable=bool(error.get("retryable", False)),
            )
        running_db = health.get("db_path")
        if running_db is not None and Path(str(running_db)) != selected_db:
            raise DuckVaultError(
                "DATABASE_SELECTION_MISMATCH",
                f"Running daemon uses {running_db}, but this command selected {selected_db}.",
            )
        return health

    try:
        client.health()
    except DuckVaultError:
        pass
    else:
        checked_health()
        return client

    if not selected_db.exists():
        raise DuckVaultError(
            "NOT_INITIALIZED",
            f"Vault is not initialized. Run 'duckvault init {layout.identity.normalized_path}'.",
        )

    layout.ensure()
    startup_lock = FileLock(str(layout.startup_lock_path))
    with startup_lock.acquire(timeout=timeout):
        try:
            layout.startup_lock_path.chmod(0o600)
        except OSError:
            pass
        try:
            client.health()
        except DuckVaultError:
            pass
        else:
            checked_health()
            return client
        if layout.endpoint_path.exists():
            # A failed health check does not prove ownership is stale. Only replace
            # metadata after the kernel-backed owner lock can be acquired.
            owner_probe = FileLock(str(layout.owner_lock_path))
            try:
                owner_probe.acquire(timeout=0)
            except Timeout:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        checked_health()
                        return client
                    except DuckVaultError:
                        time.sleep(0.05)
                raise DuckVaultError(
                    "DAEMON_UNRESPONSIVE",
                    "Daemon owns the Vault but its endpoint is not responding.",
                    retryable=True,
                )
            else:
                owner_probe.release()
                try:
                    layout.endpoint_path.unlink()
                except OSError:
                    pass
        command = [
            sys.executable,
            "-m",
            "mcp_duckvault.cli",
            "_daemon-run",
            str(layout.identity.normalized_path),
        ]
        if db_path:
            command.extend(["--db-path", db_path])
        log_handle = layout.daemon_log_path.open("ab")
        try:
            layout.daemon_log_path.chmod(0o600)
        except OSError:
            pass
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": log_handle,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)
        log_handle.close()

        deadline = time.monotonic() + timeout
        last_error: DuckVaultError | None = None
        while time.monotonic() < deadline:
            try:
                health = checked_health()
                if health.get("state") == "starting":
                    time.sleep(0.05)
                    continue
                return client
            except DuckVaultError as exc:
                last_error = exc
                time.sleep(0.05)
        raise DuckVaultError(
            "DAEMON_START_TIMEOUT",
            f"Daemon did not become reachable; inspect {layout.daemon_log_path} "
            f"({last_error.code if last_error else 'unknown'}).",
            retryable=True,
        )
