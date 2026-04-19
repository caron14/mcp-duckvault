import json
import logging
import os
import urllib.parse
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .db_manager import DatabaseManager
from .indexer import EmbeddingModel

logger = logging.getLogger(__name__)


def create_mcp_server(
    vault_path: str, db_manager: DatabaseManager, model: EmbeddingModel
) -> FastMCP:
    """Creates and configures the FastMCP server instance for DuckVault.

    This function sets up the MCP server with two primary tools:
    `search_notes` and `list_recent_notes`. It also manages the database
    connections for these tools.

    Args:
        vault_path (str): The absolute path to the Obsidian Vault.
        db_manager (DatabaseManager): The database manager instance.
        model (EmbeddingModel): The embedding model instance for similarity search.

    Returns:
        FastMCP: A configured FastMCP server instance.
    """
    mcp = FastMCP("DuckVault-MCP")

    vault_name = os.path.basename(os.path.abspath(vault_path))
    db = db_manager

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

        db.connect()
        try:
            # 1. Encode query
            query_vec = model.encode([query], is_query=True)[0]

            # 2. Build SQL
            # We use DuckDB's vss similarity search
            sql = """
                SELECT 
                    c.document_path,
                    c.content,
                    1 - (c.embedding <=> ?::FLOAT[]) as similarity,
                    d.metadata
                FROM chunks c
                JOIN documents d
                    ON c.document_path = d.path
                WHERE 1=1
            """
            params = [query_vec]

            if tag:
                # Enhanced tag filtering for various frontmatter formats:
                # 1. JSON array contains tag: ["tag1", "tag2"]
                # 2. JSON string matches tag: "tag1"
                # 3. Space-separated string contains tag: "tag1 tag2"
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

            results = db.conn.execute(sql, params).fetchall()

            if not results:
                return "No matching notes found."

            formatted_results = []
            for path, content, score, meta_json in results:
                # Generate Obsidian URI
                encoded_path = urllib.parse.quote(path)
                obsidian_uri = (
                    f"obsidian://open?vault={urllib.parse.quote(vault_name)}&file={encoded_path}"
                )

                formatted_results.append(
                    f"### File: {path} (Similarity: {score:.4f})\n"
                    f"**Link:** [{path}]({obsidian_uri})\n\n"
                    f"{content}\n"
                )

            return "\n---\n".join(formatted_results)

        except Exception as e:
            logger.error(f"Search failed: {e}")
            return f"Error during search: {str(e)}"
        finally:
            db.close()

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

        db.connect()
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
                # Generate Obsidian URI
                encoded_path = urllib.parse.quote(path)
                obsidian_uri = (
                    f"obsidian://open?vault={urllib.parse.quote(vault_name)}&file={encoded_path}"
                )
                output.append(
                    f"- {path} (Updated: {updated_at}) - [Open in Obsidian]({obsidian_uri})"
                )

            return "\n".join(output)

        except Exception as e:
            logger.error(f"Failed to list recent notes: {e}")
            return f"Error: {str(e)}"
        finally:
            db.close()

    return mcp
