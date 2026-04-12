# DuckVault-MCP

DuckVault-MCP is an MCP (Model Context Protocol) server that provides a RAG (Retrieval-Augmented Generation) system for your Obsidian Vault using DuckDB and vector search.

## Features

- **Local Vector Search**: Uses `sentence-transformers` (`intfloat/multilingual-e5-small`) for local embeddings.
- **Incremental Indexing**: Uses MD5 hashing to only update modified files.
- **Real-time Monitoring**: Uses `watchdog` to index changes as you edit your notes.
- **Fast Search**: Leverages DuckDB with the `vss` extension and HNSW indexing for millisecond-level retrieval.
- **MCP Compatible**: Works seamlessly with AI agents like Claude Code or Cursor.

## Installation

```bash
# Using uv (recommended)
uv tool install mcp-duckvault

# From GitHub directly
uv tool install git+https://github.com/caron14/mcp-obsidian-duckdb.git

# From PyPI (once published)
pip install mcp-duckvault
```

## Usage

Start the MCP server by pointing it to your Obsidian Vault:

```bash
duckvault /path/to/your/obsidian/vault
```

### Options

- `--db-path`: Path to the DuckDB file (default: `vault.db`).
- `--sync-only`: Perform a full sync of the vault and exit.
- `-v, --verbose`: Enable verbose logging.

## MCP Tools

The server exposes the following tools to AI agents:

1. `search_notes(query: str, tag: Optional[str] = None)`: Search for relevant notes using natural language.
2. `list_recent_notes(days: int = 7)`: List notes that were recently updated.

## License

MIT
