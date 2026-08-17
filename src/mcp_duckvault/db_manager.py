"""DuckDB lifecycle, schema metadata, and Vault identity enforcement."""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from .errors import DuckVaultError, VaultIdentityError
from .vault_identity import VaultIdentity

if TYPE_CHECKING:
    from .sync_status import IndexResult, SyncSummary

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PARSER_VERSION = "1"
CHUNKER_VERSION = "headers-h1-h3-v1"
GRAPH_EXTRACTOR_VERSION = "1"
DEFAULT_MODEL_ID = "intfloat/multilingual-e5-small"


def build_index_signature(model_id: str, embedding_dim: int) -> str:
    import hashlib

    payload = json.dumps(
        {
            "parser": PARSER_VERSION,
            "chunker": CHUNKER_VERSION,
            "graph_extractor": GRAPH_EXTRACTOR_VERSION,
            "embedding_model": model_id,
            "embedding_dimension": embedding_dim,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DatabaseManager:
    """Own a DuckDB connection and validate its association with one Vault."""

    def __init__(
        self,
        db_path: str = "vault.db",
        *,
        identity: VaultIdentity | None = None,
        read_only: bool = False,
    ):
        self.db_path = db_path
        self.identity = identity
        self.read_only = read_only
        self.conn: duckdb.DuckDBPyConnection | None = None

    @staticmethod
    def prepare_vss(db_path: str) -> None:
        """Install VSS during explicit initialization, never normal startup."""
        conn = duckdb.connect(db_path)
        try:
            conn.execute("INSTALL vss")
            conn.execute("LOAD vss")
        finally:
            conn.close()

    def connect(self, *, load_vss: bool = True) -> None:
        if self.conn is not None:
            return
        logger.info("Connecting to DuckDB at %s", self.db_path)
        self.conn = duckdb.connect(self.db_path, read_only=self.read_only)
        try:
            if load_vss:
                try:
                    self.conn.execute("LOAD vss")
                except Exception:
                    # Ephemeral databases are used by the test/developer API and have
                    # no persistent initialization phase. File-backed normal startup
                    # must never install or access the network here.
                    if self.db_path != ":memory:" or self.read_only:
                        raise
                    self.conn.execute("INSTALL vss")
                    self.conn.execute("LOAD vss")
                try:
                    self.conn.execute("SET hnsw_enable_experimental_persistence = true")
                except Exception as exc:
                    logger.debug("Could not enable HNSW persistence: %s", type(exc).__name__)
            if self.identity and self._table_exists("system_config"):
                self.verify_vault_identity(require_present=False)
        except Exception:
            self.close()
            raise

    def _table_exists(self, table: str) -> bool:
        assert self.conn is not None
        return (
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
            ).fetchone()
            is not None
        )

    def _config(self, key: str) -> str | None:
        assert self.conn is not None
        if not self._table_exists("system_config"):
            return None
        row = self.conn.execute("SELECT value FROM system_config WHERE key = ?", [key]).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: object) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO system_config VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, str(value)],
        )

    def verify_vault_identity(self, *, require_present: bool = True) -> None:
        """Reject a database belonging to a different or legacy Vault."""
        if self.identity is None:
            return
        stored_id = self._config("vault_id")
        stored_path = self._config("vault_path")
        if stored_id is None:
            if require_present:
                raise VaultIdentityError(
                    "Database has no Vault identity. Run 'duckvault migrate-legacy VAULT_PATH'."
                )
            return
        if stored_id != self.identity.vault_id or stored_path != str(self.identity.normalized_path):
            raise VaultIdentityError(
                "The selected database belongs to another Vault "
                f"(stored={stored_path!r}, requested={str(self.identity.normalized_path)!r})."
            )

    def initialize_schema(
        self,
        embedding_dim: int = 384,
        *,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        if self.conn is None:
            self.connect()
        assert self.conn is not None
        signature = build_index_signature(model_id, embedding_dim)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key VARCHAR PRIMARY KEY,
                value VARCHAR
            )
        """)

        stored_id = self._config("vault_id")
        if self.identity and stored_id is None:
            has_documents = self._table_exists("documents") and bool(
                self.conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
            )
            if has_documents:
                raise VaultIdentityError(
                    "Legacy database cannot be claimed automatically. "
                    "Run 'duckvault migrate-legacy VAULT_PATH'."
                )
            self.set_config("vault_id", self.identity.vault_id)
            self.set_config("vault_path", self.identity.normalized_path)
            self.set_config("created_at", datetime.now(timezone.utc).isoformat())
        self.verify_vault_identity(require_present=self.identity is not None)
        stored_schema = self._config("schema_version")
        if stored_schema is not None:
            try:
                parsed_schema = int(stored_schema)
            except ValueError as exc:
                raise DuckVaultError(
                    "SCHEMA_UNSUPPORTED", f"Invalid database schema version: {stored_schema!r}."
                ) from exc
            if parsed_schema > SCHEMA_VERSION:
                raise DuckVaultError(
                    "SCHEMA_UNSUPPORTED",
                    f"Database schema {parsed_schema} is newer than supported {SCHEMA_VERSION}.",
                )

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                path VARCHAR PRIMARY KEY,
                md5 VARCHAR,
                metadata JSON,
                source_modified_at TIMESTAMP,
                index_signature VARCHAR,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Upgrade databases created by v0.3 before version metadata existed.
        self.conn.execute(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_modified_at TIMESTAMP"
        )
        self.conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_signature VARCHAR")
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id VARCHAR PRIMARY KEY,
                document_path VARCHAR,
                content TEXT,
                embedding FLOAT[{embedding_dim}],
                metadata JSON
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id VARCHAR PRIMARY KEY,
                node_type VARCHAR NOT NULL,
                name VARCHAR NOT NULL,
                document_path VARCHAR,
                metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS edges (
                edge_id VARCHAR PRIMARY KEY,
                source_node_id VARCHAR NOT NULL,
                target_node_id VARCHAR NOT NULL,
                edge_type VARCHAR NOT NULL,
                weight DOUBLE DEFAULT 1.0,
                document_path VARCHAR,
                metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS node_mentions (
                mention_id VARCHAR PRIMARY KEY,
                node_id VARCHAR NOT NULL,
                chunk_id VARCHAR,
                document_path VARCHAR NOT NULL,
                mention_text VARCHAR,
                metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS edge_nodes_idx ON edges (source_node_id, target_node_id);
            CREATE INDEX IF NOT EXISTS edge_document_idx ON edges (document_path);
            CREATE INDEX IF NOT EXISTS node_document_idx ON nodes (document_path);
            CREATE INDEX IF NOT EXISTS mention_node_chunk_idx ON node_mentions (node_id, chunk_id);
            CREATE INDEX IF NOT EXISTS mention_document_idx ON node_mentions (document_path)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id VARCHAR PRIMARY KEY,
                status VARCHAR NOT NULL,
                scanned BIGINT NOT NULL,
                indexed BIGINT NOT NULL,
                skipped BIGINT NOT NULL,
                deleted BIGINT NOT NULL,
                failed BIGINT NOT NULL,
                excluded BIGINT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_failures (
                path VARCHAR PRIMARY KEY,
                error_code VARCHAR NOT NULL,
                error_type VARCHAR NOT NULL,
                occurred_at TIMESTAMP NOT NULL
            )
        """)

        try:
            existing = self.conn.execute(
                "SELECT 1 FROM duckdb_indexes() WHERE index_name = 'chunk_vec_idx'"
            ).fetchone()
            if not existing:
                self.conn.execute(
                    "CREATE INDEX chunk_vec_idx ON chunks USING HNSW (embedding) "
                    "WITH (metric = 'cosine')"
                )
        except Exception as exc:
            logger.warning("Could not create HNSW index (%s)", type(exc).__name__)

        previous_signature = self._config("index_signature")
        self.set_config("schema_version", SCHEMA_VERSION)
        self.set_config("parser_version", PARSER_VERSION)
        self.set_config("chunker_version", CHUNKER_VERSION)
        self.set_config("graph_extractor_version", GRAPH_EXTRACTOR_VERSION)
        self.set_config("embedding_model_id", model_id)
        self.set_config("embedding_dimension", embedding_dim)
        self.set_config("index_signature", signature)
        if previous_signature and previous_signature != signature:
            self.set_config("index_state", "reindex_required")
        elif not self._config("index_state"):
            self.set_config("index_state", "ready")

    def record_index_result(self, result: "IndexResult") -> None:
        assert self.conn is not None
        if result.status.value == "failed":
            self.conn.execute(
                "INSERT INTO sync_failures VALUES (?, ?, ?, ?) "
                "ON CONFLICT (path) DO UPDATE SET error_code=excluded.error_code, "
                "error_type=excluded.error_type, occurred_at=excluded.occurred_at",
                [result.path, result.error_code, result.error_type, result.occurred_at],
            )
        else:
            self.conn.execute("DELETE FROM sync_failures WHERE path = ?", [result.path])

    def record_sync(self, run_id: str, summary: "SyncSummary") -> None:
        assert self.conn is not None and summary.finished_at is not None
        self.conn.execute(
            "INSERT INTO sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                summary.status,
                summary.scanned,
                summary.indexed,
                summary.skipped,
                summary.deleted,
                summary.failed,
                summary.excluded,
                summary.started_at,
                summary.finished_at,
            ],
        )
        self.set_config("index_state", "ready" if summary.status == "complete" else "degraded")

    def status(self, *, failure_limit: int = 100) -> dict[str, object]:
        assert self.conn is not None
        last = self.conn.execute(
            "SELECT status, scanned, indexed, skipped, deleted, failed, excluded, "
            "started_at, finished_at FROM sync_runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        failures = self.conn.execute(
            "SELECT path, error_code, error_type, occurred_at FROM sync_failures "
            "ORDER BY occurred_at DESC LIMIT ?",
            [max(0, failure_limit)],
        ).fetchall()
        sync = None
        if last:
            sync = {
                "status": last[0],
                "scanned": last[1],
                "indexed": last[2],
                "skipped": last[3],
                "deleted": last[4],
                "failed": last[5],
                "excluded": last[6],
                "started_at": last[7].isoformat(),
                "finished_at": last[8].isoformat(),
            }
        return {
            "index_state": self._config("index_state") or "unknown",
            "vault_id": self._config("vault_id"),
            "vault_path": self._config("vault_path"),
            "schema_version": self._config("schema_version"),
            "last_sync": sync,
            "failures": [
                {
                    "path": row[0],
                    "error_code": row[1],
                    "error_type": row[2],
                    "occurred_at": row[3].isoformat(),
                }
                for row in failures
            ],
        }

    def backup(self, destination: Path) -> Path:
        """Checkpoint and copy a closed-form database backup."""
        if self.conn is None:
            self.connect(load_vss=False)
        assert self.conn is not None
        self.conn.execute("CHECKPOINT")
        self.close()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(self.db_path, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        return destination

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            logger.info("Database connection closed")


def safe_database_permissions(db_path: str | os.PathLike[str]) -> None:
    try:
        Path(db_path).chmod(0o600)
    except OSError:
        pass


def require_schema_version(db: DatabaseManager) -> None:
    version = db._config("schema_version")
    if version is None or int(version) > SCHEMA_VERSION:
        raise DuckVaultError(
            "SCHEMA_UNSUPPORTED",
            f"Unsupported database schema version: {version!r}.",
        )
