import json
import logging
import os
import urllib.parse
from typing import Optional

import duckdb
from mcp.server.fastmcp import FastMCP

from .db_manager import DatabaseManager
from .graph_extractor import GraphExtractor
from .graph_repository import GraphRepository
from .indexer import EmbeddingModel

logger = logging.getLogger(__name__)


def _obsidian_uri(vault_name: str, path: str) -> str:
    return (
        f"obsidian://open?vault={urllib.parse.quote(vault_name)}"
        f"&file={urllib.parse.quote(path)}"
    )


def _vector_search(
    conn: duckdb.DuckDBPyConnection, query_vec: list[float], tag: str | None, limit: int
) -> list[dict]:
    if limit < 1:
        return []
    sql = """
        SELECT
            c.document_path,
            c.content,
            1 - (c.embedding <=> ?::FLOAT[]) AS similarity,
            d.metadata
        FROM chunks c
        JOIN documents d ON c.document_path = d.path
        WHERE 1=1
    """
    params = [query_vec]
    if tag:
        sql += """ AND (
            json_contains(d.metadata->'$.tags', ?) OR
            json_contains(d.metadata->'$.tag', ?) OR
            CAST(d.metadata->>'$.tags' AS VARCHAR) = ? OR
            CAST(d.metadata->>'$.tag' AS VARCHAR) = ? OR
            contains(CAST(d.metadata->>'$.tags' AS VARCHAR), ?) OR
            contains(CAST(d.metadata->>'$.tag' AS VARCHAR), ?)
        )"""
        tag_json = json.dumps(tag)
        params.extend([tag_json, tag_json, tag, tag, tag, tag])
    sql += " ORDER BY similarity DESC LIMIT ?"
    params.append(limit)
    return [
        {"path": row[0], "content": row[1], "similarity": row[2], "metadata": row[3]}
        for row in conn.execute(sql, params).fetchall()
    ]


def create_mcp_server(
    vault_path: str, db_manager: DatabaseManager, model: EmbeddingModel
) -> FastMCP:
    """Creates and configures the FastMCP server instance for DuckVault.

    This function exposes vector, graph, hybrid, and OKF retrieval tools and
    manages their database connections.

    Args:
        vault_path (str): The absolute path to the Obsidian Vault.
        db_manager (DatabaseManager): The database manager instance.
        model (EmbeddingModel): The embedding model instance for similarity search.

    Returns:
        FastMCP: A configured FastMCP server instance.
    """
    mcp = FastMCP("DuckVault-MCP")

    vault_name = os.path.basename(os.path.abspath(vault_path))
    owns_connection = db_manager.db_path != ":memory:"
    db = DatabaseManager(db_manager.db_path) if owns_connection else db_manager

    def connect() -> None:
        if db.conn is None:
            db.connect()

    def close() -> None:
        if owns_connection:
            db.close()

    @mcp.tool()
    async def search_notes(query: str, tag: Optional[str] = None, limit: int = 5) -> str:
        """Searches for notes in the vault using vector similarity.

        Encodes the natural language query into a vector and performs an
        HNSW similarity search against the indexed chunks in DuckDB.
        Optional tag filtering is supported for various frontmatter formats.

        Args:
            query (str): The natural language search query.
            tag (Optional[str]): A tag to filter notes by. Searches in the
                'tags' or 'tag' fields of the document metadata.
            limit (int): The maximum number of results to return. Defaults to 5.

        Returns:
            str: A formatted Markdown string containing the search results,
                including Obsidian URIs and content snippets.
        """
        logger.info(f"Searching notes for query: '{query}', tag: {tag}")

        connect()
        try:
            # 1. Encode query
            query_vec = model.encode([query], is_query=True)[0]

            results = _vector_search(db.conn, query_vec, tag, limit)

            if not results:
                return "No matching notes found."

            formatted_results = []
            for result in results:
                path = result["path"]
                formatted_results.append(
                    f"### File: {path} (Similarity: {result['similarity']:.4f})\n"
                    f"**Link:** [{path}]({_obsidian_uri(vault_name, path)})\n\n"
                    f"{result['content']}\n"
                )

            return "\n---\n".join(formatted_results)

        except Exception as e:
            logger.error(f"Search failed: {e}")
            return f"Error during search: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def list_recent_notes(days: int = 7) -> str:
        """Lists notes that have been modified within a specific timeframe.

        Queries the database for documents whose 'updated_at' timestamp
        falls within the last N days.

        Args:
            days (int): The number of days to look back. Defaults to 7.

        Returns:
            str: A formatted Markdown string listing the recent notes
                with Obsidian URIs and their last updated timestamps.
        """
        logger.info(f"Listing notes modified in the last {days} days")

        connect()
        try:
            # Using casting to handle interval with parameters in DuckDB
            sql = """
                SELECT path, updated_at, metadata
                FROM documents
                WHERE updated_at >= CURRENT_TIMESTAMP - (INTERVAL '1 day' * ?)
                ORDER BY updated_at DESC
            """
            results = db.conn.execute(sql, (days,)).fetchall()

            if not results:
                return f"No notes modified in the last {days} days."

            output = [f"Recent notes (last {days} days):"]
            for path, updated_at, meta_json in results:
                output.append(
                    f"- {path} (Updated: {updated_at}) - "
                    f"[Open in Obsidian]({_obsidian_uri(vault_name, path)})"
                )

            return "\n".join(output)

        except Exception as e:
            logger.error(f"Failed to list recent notes: {e}")
            return f"Error: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def find_related_notes(path: str, depth: int = 1, limit: int = 10) -> str:
        """Find notes connected to a note through Markdown and OKF relationships."""
        connect()
        try:
            normalized = GraphExtractor.normalize_path(path)
            neighbors = GraphRepository(db).find_neighbors(
                normalized, depth=max(1, depth), limit=max(limit * 5, limit)
            )
            related = [
                item
                for item in neighbors
                if item["node_type"] in {"document", "okf_concept", "okf_index", "okf_log"}
                and item["document_path"] != normalized
            ][: max(0, limit)]
            if not related:
                return "No related notes found."
            output = []
            for item in related:
                note_path = item["document_path"]
                reason = (
                    f"{item['direction']} {item['edge_type']} relation "
                    f"at graph depth {item['depth']}"
                )
                output.append(
                    f"### {note_path}\n"
                    f"- Relation: `{item['edge_type']}`\n"
                    f"- Depth: {item['depth']}\n"
                    f"- Score: {item['graph_score']:.4f}\n"
                    f"- Reason: {reason}\n"
                    f"- Link: [{note_path}]({_obsidian_uri(vault_name, note_path)})"
                )
            return "\n\n".join(output)
        except Exception as e:
            logger.error(f"Related-note search failed: {e}")
            return f"Error during graph search: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def list_graph_neighbors(path: str, depth: int = 1, limit: int = 20) -> str:
        """List graph nodes neighboring a Markdown note."""
        connect()
        try:
            neighbors = GraphRepository(db).find_neighbors(
                path, depth=max(1, depth), limit=max(0, limit)
            )
            if not neighbors:
                return "No graph neighbors found."
            output = []
            for item in neighbors:
                summary = json.dumps(item["metadata"], ensure_ascii=False, default=str)
                output.append(
                    f"- **{item['name']}** (`{item['node_type']}`) — "
                    f"`{item['edge_type']}`, depth {item['depth']}, metadata: {summary}"
                )
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Neighbor listing failed: {e}")
            return f"Error during graph search: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def hybrid_search_notes(
        query: str,
        tag: Optional[str] = None,
        limit: int = 5,
        graph_depth: int = 1,
    ) -> str:
        """Combine vector similarity with graph expansion from the vector results."""
        connect()
        try:
            query_vec = model.encode([query], is_query=True)[0]
            vector_rows = _vector_search(db.conn, query_vec, tag, max(limit * 3, limit))
            if not vector_rows:
                return "No matching notes found."

            documents: dict[str, dict] = {}
            for row in vector_rows:
                current = documents.get(row["path"])
                if current is None or row["similarity"] > current["vector_similarity"]:
                    documents[row["path"]] = {
                        "path": row["path"],
                        "content": row["content"],
                        "vector_similarity": row["similarity"],
                        "graph_score": 0.0,
                        "relation": None,
                    }

            repository = GraphRepository(db)
            seed_rows = list(documents.values())[: max(1, limit)]
            for seed in seed_rows:
                neighbors = repository.find_neighbors(
                    seed["path"], depth=max(1, graph_depth), limit=max(limit * 10, 20)
                )
                for neighbor in neighbors:
                    note_path = neighbor.get("document_path")
                    if neighbor["node_type"] not in {"document", "okf_concept"} or not note_path:
                        continue
                    graph_score = seed["vector_similarity"] / neighbor["depth"]
                    candidate = documents.setdefault(
                        note_path,
                        {
                            "path": note_path,
                            "content": "",
                            "vector_similarity": 0.0,
                            "graph_score": 0.0,
                            "relation": None,
                        },
                    )
                    if graph_score > candidate["graph_score"]:
                        candidate["graph_score"] = graph_score
                        candidate["relation"] = neighbor["edge_type"]

            missing = [item["path"] for item in documents.values() if not item["content"]]
            if missing:
                placeholders = ", ".join("?" for _ in missing)
                rows = db.conn.execute(
                    f"""
                    SELECT document_path, first(content)
                    FROM chunks
                    WHERE document_path IN ({placeholders})
                    GROUP BY document_path
                    """,
                    missing,
                ).fetchall()
                for note_path, content in rows:
                    documents[note_path]["content"] = content

            for item in documents.values():
                item["final_score"] = 0.7 * item["vector_similarity"] + 0.3 * item["graph_score"]
            ranked = sorted(documents.values(), key=lambda item: item["final_score"], reverse=True)[
                : max(0, limit)
            ]
            output = []
            for item in ranked:
                relation = f", relation: {item['relation']}" if item["relation"] else ""
                output.append(
                    f"### {item['path']} (Score: {item['final_score']:.4f})\n"
                    f"Vector: {item['vector_similarity']:.4f}, "
                    f"Graph: {item['graph_score']:.4f}{relation}\n"
                    f"**Link:** [{item['path']}]"
                    f"({_obsidian_uri(vault_name, item['path'])})\n\n"
                    f"{item['content']}"
                )
            return "\n---\n".join(output)
        except Exception as e:
            logger.error(f"Hybrid search failed: {e}")
            return f"Error during hybrid search: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def search_okf_concepts(
        okf_type: Optional[str] = None, tag: Optional[str] = None, limit: int = 20
    ) -> str:
        """Search OKF Concept Documents by type and tag."""
        connect()
        try:
            concepts = GraphRepository(db).search_okf_concepts(okf_type, tag, max(0, limit))
            if not concepts:
                return "No matching OKF concepts found."
            output = []
            for concept in concepts:
                path = concept["document_path"]
                output.append(
                    f"### {concept['title']} (`{concept['concept_id']}`)\n"
                    f"- Type: {concept['type'] or '-'}\n"
                    f"- Description: {concept['description'] or '-'}\n"
                    f"- Resource: {concept['resource'] or '-'}\n"
                    f"- Tags: {', '.join(concept['tags']) or '-'}\n"
                    f"- Path: {path}\n"
                    f"- Link: [{path}]({_obsidian_uri(vault_name, path)})"
                )
            return "\n\n".join(output)
        except Exception as e:
            logger.error(f"OKF concept search failed: {e}")
            return f"Error during OKF concept search: {str(e)}"
        finally:
            close()

    @mcp.tool()
    async def explain_okf_concept(concept_id: str) -> str:
        """Explain an OKF concept and its graph relationships."""
        connect()
        try:
            concept = GraphRepository(db).find_okf_concept(concept_id)
            if not concept:
                return f"OKF concept not found: {concept_id}"
            path = concept["document_path"]
            linked = ", ".join(item["document_path"] for item in concept["linked_concepts"]) or "-"
            related = ", ".join(item["document_path"] for item in concept["related_notes"]) or "-"
            citations = ", ".join(item["name"] for item in concept["citations"]) or "-"
            return (
                f"# {concept['title']} (`{concept['concept_id']}`)\n\n"
                f"- Type: {concept['type'] or '-'}\n"
                f"- Description: {concept['description'] or '-'}\n"
                f"- Timestamp: {concept['timestamp'] or '-'}\n"
                f"- Resource: {concept['resource'] or '-'}\n"
                f"- Tags: {', '.join(concept['tags']) or '-'}\n"
                f"- Headings: {', '.join(concept['headings']) or '-'}\n"
                f"- Linked concepts: {linked}\n"
                f"- Related notes: {related}\n"
                f"- Citations: {citations}\n"
                f"- Link: [{path}]({_obsidian_uri(vault_name, path)})"
            )
        except Exception as e:
            logger.error(f"OKF concept explanation failed: {e}")
            return f"Error during OKF concept explanation: {str(e)}"
        finally:
            close()

    return mcp
