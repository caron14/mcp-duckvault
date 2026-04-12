import logging
import json
from typing import List, Optional, Dict, Any
from mcp.server.fastmcp import FastMCP
from .db_manager import DatabaseManager
from .indexer import EmbeddingModel

logger = logging.getLogger(__name__)

def create_mcp_server(db_path: str) -> FastMCP:
    """Create and configure the FastMCP server instance."""
    mcp = FastMCP("DuckVault-MCP")
    
    # We'll initialize these lazily or via a shared context if needed,
    # but for simplicity we'll open a connection per tool or use a global one.
    db = DatabaseManager(db_path)
    model = EmbeddingModel()

    @mcp.tool()
    async def search_notes(query: str, tag: Optional[str] = None, limit: int = 5) -> str:
        """
        Search for notes in the vault using vector similarity.
        
        Args:
            query: The natural language search query.
            tag: Optional tag to filter by (searches in document metadata).
            limit: Number of results to return (default 5).
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
                JOIN documents d ON c.document_path = d.path
                WHERE 1=1
            """
            params = [query_vec]
            
            if tag:
                # Assuming tag is in the metadata JSON array/object
                # DuckDB JSON extraction: metadata->'$.tags' contains tag
                # This is a bit flexible depending on how user stores tags in frontmatter
                sql += " AND (d.metadata->'$.tags' ? ? OR d.metadata->'$.tag' = ?)"
                params.extend([tag, tag])
            
            sql += " ORDER BY similarity DESC LIMIT ?"
            params.append(limit)
            
            results = db.conn.execute(sql, params).fetchall()
            
            if not results:
                return "No matching notes found."
            
            formatted_results = []
            for path, content, score, meta_json in results:
                formatted_results.append(
                    f"### File: {path} (Similarity: {score:.4f})\n"
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
        """
        List notes that have been modified within the last N days.
        
        Args:
            days: Number of days to look back (default 7).
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
                output.append(f"- {path} (Updated: {updated_at})")
            
            return "\n".join(output)
            
        except Exception as e:
            logger.error(f"Failed to list recent notes: {e}")
            return f"Error: {str(e)}"
        finally:
            db.close()

    return mcp
