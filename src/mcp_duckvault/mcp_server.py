"""Structured MCP retrieval tools backed by one DuckVault database."""

import json
import logging
import os
import re
import urllib.parse
from typing import Any, Optional

import duckdb
from mcp.server.fastmcp import FastMCP

from .db_manager import DatabaseManager
from .errors import DuckVaultError
from .graph_extractor import GraphExtractor
from .graph_repository import GraphRepository
from .indexer import EmbeddingModel

logger = logging.getLogger(__name__)

TOOL_SCHEMA_VERSION = "1.0"
MAX_QUERY_CHARS = 4096
MAX_PATH_CHARS = 4096
MAX_RESULTS = 100
MAX_DEPTH = 5
MAX_DAYS = 3650
MAX_SNIPPET_CHARS = 2000


def _obsidian_uri(vault_name: str, path: str) -> str:
    return (
        f"obsidian://open?vault={urllib.parse.quote(vault_name)}"
        f"&file={urllib.parse.quote(path)}"
    )


def _required_text(name: str, value: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise DuckVaultError("INVALID_ARGUMENT", f"{name} must not be empty.")
    if len(normalized) > maximum:
        raise DuckVaultError("ARGUMENT_TOO_LARGE", f"{name} must be at most {maximum} characters.")
    return normalized


def _bounded_int(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DuckVaultError("INVALID_ARGUMENT", f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise DuckVaultError("INVALID_ARGUMENT", f"{name} must be between {minimum} and {maximum}.")
    return value


def _normalize_tag(value: object) -> str:
    return str(value).strip().lstrip("#").casefold()


def _metadata_tags(metadata: object) -> set[str]:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return set()
    if not isinstance(metadata, dict):
        return set()
    values: list[object] = []
    for key in ("tag", "tags"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif isinstance(raw, str):
            values.extend(item for item in re.split(r"[,\s]+", raw) if item)
    return {_normalize_tag(item) for item in values if _normalize_tag(item)}


def _heading(content: str) -> str | None:
    for line in content.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


def _snippet(content: str) -> str:
    return content[:MAX_SNIPPET_CHARS]


def _result(tool: str, items: list[dict[str, Any]], **extra: object) -> dict[str, object]:
    return {
        "schema_version": TOOL_SCHEMA_VERSION,
        "tool": tool,
        "count": len(items),
        "items": items,
        **extra,
    }


def _vector_search(
    conn: duckdb.DuckDBPyConnection,
    query_vec: list[float],
    tag: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the best chunk per document, with exact normalized tag filtering."""
    rows = conn.execute(
        """
        SELECT c.document_path, c.content,
               1 - (c.embedding <=> ?::FLOAT[]) AS similarity,
               d.metadata
        FROM chunks c
        JOIN documents d ON c.document_path = d.path
        ORDER BY similarity DESC, c.document_path
        """,
        [query_vec],
    ).fetchall()
    expected_tag = _normalize_tag(tag) if tag else None
    documents: dict[str, dict[str, Any]] = {}
    for path, content, similarity, metadata in rows:
        if expected_tag and expected_tag not in _metadata_tags(metadata):
            continue
        if path in documents:
            continue
        parsed_metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        documents[path] = {
            "path": path,
            "heading": _heading(content),
            "snippet": _snippet(content),
            "score": float(similarity),
            "reason": "best_vector_chunk",
            "metadata": parsed_metadata or {},
        }
        if len(documents) >= limit:
            break
    return list(documents.values())


def create_mcp_server(
    vault_path: str,
    db_manager: DatabaseManager,
    model: EmbeddingModel,
    *,
    manage_connection: bool | None = None,
) -> FastMCP:
    """Create retrieval tools that return versioned, bounded structured data."""
    mcp = FastMCP("DuckVault-MCP")
    vault_name = os.path.basename(os.path.abspath(vault_path))
    owns_connection = (
        db_manager.db_path != ":memory:" if manage_connection is None else manage_connection
    )
    db = DatabaseManager(db_manager.db_path) if owns_connection else db_manager

    def connect() -> None:
        if db.conn is None:
            db.connect()

    def close() -> None:
        if owns_connection:
            db.close()

    def link(path: str) -> str:
        return _obsidian_uri(vault_name, path)

    @mcp.tool()
    async def search_notes(
        query: str, tag: Optional[str] = None, limit: int = 5
    ) -> dict[str, object]:
        """Search notes using vector similarity and an exact normalized tag."""
        query = _required_text("query", query, maximum=MAX_QUERY_CHARS)
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        normalized_tag = _normalize_tag(tag) if tag else None
        connect()
        try:
            vector = model.encode([query], is_query=True)[0]
            items = _vector_search(db.conn, vector, normalized_tag, limit)
            for item in items:
                item["link"] = link(str(item["path"]))
            return _result("search_notes", items, query=query, tag=normalized_tag)
        except DuckVaultError:
            raise
        except Exception as exc:
            logger.error("Search failed (%s)", type(exc).__name__)
            raise DuckVaultError("SEARCH_FAILED", "Vector search failed.", retryable=True) from exc
        finally:
            close()

    @mcp.tool()
    async def list_recent_notes(days: int = 7, limit: int = 20) -> dict[str, object]:
        """List notes by their source filesystem modification timestamp."""
        days = _bounded_int("days", days, minimum=0, maximum=MAX_DAYS)
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        connect()
        try:
            rows = db.conn.execute(
                """
                SELECT path, source_modified_at, metadata
                FROM documents
                WHERE source_modified_at IS NOT NULL
                  AND source_modified_at >= CURRENT_TIMESTAMP - (INTERVAL '1 day' * ?)
                ORDER BY source_modified_at DESC, path
                LIMIT ?
                """,
                (days, limit),
            ).fetchall()
            items = [
                {
                    "path": path,
                    "source_modified_at": modified.isoformat(),
                    "metadata": json.loads(metadata) if isinstance(metadata, str) else metadata,
                    "link": link(path),
                }
                for path, modified, metadata in rows
            ]
            return _result("list_recent_notes", items, days=days)
        except Exception as exc:
            logger.error("Recent-note listing failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "RECENT_NOTES_FAILED", "Recent-note listing failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def find_related_notes(path: str, depth: int = 1, limit: int = 10) -> dict[str, object]:
        """Find document-like nodes related through graph edges."""
        path = _required_text("path", path, maximum=MAX_PATH_CHARS)
        depth = _bounded_int("depth", depth, minimum=1, maximum=MAX_DEPTH)
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        normalized = GraphExtractor.normalize_path(path)
        connect()
        try:
            neighbors = GraphRepository(db).find_neighbors(
                normalized, depth=depth, limit=min(limit * 5, MAX_RESULTS * 5)
            )
            items = []
            for item in neighbors:
                if (
                    item["node_type"]
                    not in {
                        "document",
                        "okf_concept",
                        "okf_index",
                        "okf_log",
                    }
                    or item["document_path"] == normalized
                ):
                    continue
                result = dict(item)
                result["reason"] = f"{item['direction']}:{item['edge_type']}:depth={item['depth']}"
                result["link"] = link(str(item["document_path"]))
                items.append(result)
                if len(items) >= limit:
                    break
            return _result("find_related_notes", items, path=normalized, depth=depth)
        except Exception as exc:
            logger.error("Related-note search failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "GRAPH_SEARCH_FAILED", "Graph search failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def list_graph_neighbors(path: str, depth: int = 1, limit: int = 20) -> dict[str, object]:
        """List graph nodes neighboring a Markdown note."""
        path = _required_text("path", path, maximum=MAX_PATH_CHARS)
        depth = _bounded_int("depth", depth, minimum=1, maximum=MAX_DEPTH)
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        normalized = GraphExtractor.normalize_path(path)
        connect()
        try:
            items = GraphRepository(db).find_neighbors(normalized, depth=depth, limit=limit)
            return _result("list_graph_neighbors", items, path=normalized, depth=depth)
        except Exception as exc:
            logger.error("Neighbor listing failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "GRAPH_SEARCH_FAILED", "Graph search failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def hybrid_search_notes(
        query: str,
        tag: Optional[str] = None,
        limit: int = 5,
        graph_depth: int = 1,
    ) -> dict[str, object]:
        """Combine document-deduplicated vector results with graph expansion."""
        query = _required_text("query", query, maximum=MAX_QUERY_CHARS)
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        graph_depth = _bounded_int("graph_depth", graph_depth, minimum=1, maximum=MAX_DEPTH)
        normalized_tag = _normalize_tag(tag) if tag else None
        connect()
        try:
            vector = model.encode([query], is_query=True)[0]
            vector_rows = _vector_search(
                db.conn, vector, normalized_tag, min(limit * 3, MAX_RESULTS)
            )
            documents = {
                row["path"]: {
                    **row,
                    "vector_score": row.pop("score"),
                    "graph_score": 0.0,
                    "relation": None,
                }
                for row in vector_rows
            }
            repository = GraphRepository(db)
            for seed in list(documents.values())[:limit]:
                for neighbor in repository.find_neighbors(
                    seed["path"], depth=graph_depth, limit=min(limit * 10, MAX_RESULTS * 5)
                ):
                    note_path = neighbor.get("document_path")
                    if neighbor["node_type"] not in {"document", "okf_concept"} or not note_path:
                        continue
                    graph_score = seed["vector_score"] / neighbor["depth"]
                    candidate = documents.setdefault(
                        note_path,
                        {
                            "path": note_path,
                            "heading": None,
                            "snippet": "",
                            "metadata": {},
                            "reason": "graph_expansion",
                            "vector_score": 0.0,
                            "graph_score": 0.0,
                            "relation": None,
                        },
                    )
                    if graph_score > candidate["graph_score"]:
                        candidate["graph_score"] = graph_score
                        candidate["relation"] = neighbor["edge_type"]
            missing = [item["path"] for item in documents.values() if not item["snippet"]]
            if missing:
                placeholders = ", ".join("?" for _ in missing)
                rows = db.conn.execute(
                    f"SELECT document_path, first(content) FROM chunks "
                    f"WHERE document_path IN ({placeholders}) GROUP BY document_path",
                    missing,
                ).fetchall()
                for note_path, content in rows:
                    documents[note_path]["snippet"] = _snippet(content)
                    documents[note_path]["heading"] = _heading(content)
            for item in documents.values():
                item["score"] = 0.7 * item["vector_score"] + 0.3 * item["graph_score"]
                item["link"] = link(item["path"])
            items = sorted(documents.values(), key=lambda item: item["score"], reverse=True)[:limit]
            return _result(
                "hybrid_search_notes",
                items,
                query=query,
                tag=normalized_tag,
                graph_depth=graph_depth,
            )
        except DuckVaultError:
            raise
        except Exception as exc:
            logger.error("Hybrid search failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "HYBRID_SEARCH_FAILED", "Hybrid search failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def search_okf_concepts(
        okf_type: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, object]:
        """Search OKF concept documents by exact type and tag."""
        limit = _bounded_int("limit", limit, minimum=1, maximum=MAX_RESULTS)
        connect()
        try:
            items = GraphRepository(db).search_okf_concepts(okf_type, tag, limit)
            for item in items:
                item["link"] = link(str(item["document_path"]))
            return _result("search_okf_concepts", items, okf_type=okf_type, tag=tag)
        except Exception as exc:
            logger.error("OKF concept search failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "OKF_SEARCH_FAILED", "OKF concept search failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def explain_okf_concept(concept_id: str) -> dict[str, object]:
        """Return one OKF concept and its graph relationships."""
        concept_id = _required_text("concept_id", concept_id, maximum=MAX_PATH_CHARS)
        connect()
        try:
            concept = GraphRepository(db).find_okf_concept(concept_id)
            items = [] if concept is None else [concept]
            if items:
                items[0]["link"] = link(str(items[0]["document_path"]))
            return _result("explain_okf_concept", items, concept_id=concept_id)
        except Exception as exc:
            logger.error("OKF concept explanation failed (%s)", type(exc).__name__)
            raise DuckVaultError(
                "OKF_EXPLAIN_FAILED", "OKF concept explanation failed.", retryable=True
            ) from exc
        finally:
            close()

    @mcp.tool()
    async def get_index_status(
        include_failures: bool = False, failure_limit: int = 100
    ) -> dict[str, object]:
        """Return index completeness and the most recent synchronization result."""
        failure_limit = _bounded_int("failure_limit", failure_limit, minimum=0, maximum=MAX_RESULTS)
        connect()
        try:
            result = db.status(failure_limit=failure_limit if include_failures else 0)
            result["schema_version"] = TOOL_SCHEMA_VERSION
            result["tool"] = "get_index_status"
            result["complete"] = result["index_state"] == "ready"
            return result
        finally:
            close()

    return mcp
