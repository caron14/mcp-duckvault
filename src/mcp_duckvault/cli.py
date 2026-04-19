import logging
import click
import sys
from .db_manager import DatabaseManager
from .indexer import VaultIndexer, start_watcher
from .mcp_server import create_mcp_server

def setup_logging(verbose: bool):
    """
    Configure the logging system for the entire application.

    CRITICAL DESIGN NOTE:
    All logs are directed to stderr instead of stdout. This is because the
    MCP (Model Context Protocol) server uses stdout for its JSON-RPC communication
    with the LLM client. Mixing log messages into stdout would corrupt the
    communication protocol.

    Args:
        verbose (bool): If True, sets logging level to DEBUG; otherwise, sets to INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr
    )

@click.command()
@click.argument("vault_path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--db-path", default="vault.db", help="Path to DuckDB file (default: vault.db)")
@click.option("--sync-only", is_flag=True, help="Perform full sync and exit without starting MCP server")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def main(vault_path: str, db_path: str, sync_only: bool, verbose: bool):
    """
    DuckVault-MCP: Obsidian-DuckDB RAG System with MCP Server.
    
    VAULT_PATH: The absolute path to your Obsidian Vault.
    """
    setup_logging(verbose)
    logger = logging.getLogger("mcp_duckvault")
    
    logger.info(f"Initializing DuckVault-MCP for {vault_path}")
    
    # 1. Initialize Database
    db = DatabaseManager(db_path)
    try:
        db.initialize_schema()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

    # 2. Perform Full Sync
    indexer = VaultIndexer(vault_path, db)
    try:
        indexer.full_sync()
    except Exception as e:
        logger.error(f"Full sync failed: {e}")
        if sync_only:
            sys.exit(1)

    if sync_only:
        logger.info("Sync complete. Exiting.")
        return

    # 3. Start Background Watcher
    observer = start_watcher(vault_path, indexer)
    
    # 4. Start MCP Server (stdio mode)
    logger.info("Starting MCP server...")
    mcp = create_mcp_server(db_path)
    
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()
        db.close()

if __name__ == "__main__":
    main()
