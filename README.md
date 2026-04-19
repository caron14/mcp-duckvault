# DuckVault-MCP

DuckVault-MCP is an MCP (Model Context Protocol) server that provides a RAG (Retrieval-Augmented Generation) system for your Obsidian Vault using DuckDB and vector search.

## Features

- **Local Vector Search**: Uses `sentence-transformers` (`intfloat/multilingual-e5-small`) for local embeddings.
- **Obsidian URI Links**: Includes direct links to open notes in Obsidian from search results.
- **Incremental Indexing**: Uses MD5 hashing to only update modified files.
- **Real-time Monitoring**: Uses `watchdog` to index changes as you edit your notes.
- **Progress Indicators**: Displays a progress bar during initial indexing for large vaults.
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

- `--db-path`: Path to the DuckDB file (default: `~/.duckvault/vault.db`).
- `--sync-only`: Perform a full sync of the vault and exit.
- `-v, --verbose`: Enable verbose logging.

### Excluding Files (`.vaultignore`)

To exclude specific files or directories from being indexed, create a `.vaultignore` file in the root of your Obsidian Vault. The syntax is similar to `.gitignore`.

By default, `.obsidian` and `.trash` are always excluded.

Example `.vaultignore`:
```text
# Exclude specific folders
private/
drafts/

# Exclude specific file types
*.tmp
*.log
```

## Registration for AI Agents

This server is MCP (Model Context Protocol) compliant and can be used with the following agents after you have installed it via `uv tool install` or `pip install`.

**Note**: Please specify the vault path as an **absolute path** (e.g., `/Users/username/Documents/MyVault`).

### Claude Desktop (macOS)
Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": [
        "/absolute/path/to/your/obsidian/vault"
      ]
    }
  }
}
```

### Claude Code
You can add the server persistently using the CLI command (recommended) or by editing the configuration file.

**Using the CLI command:**
```bash
claude mcp add duckvault -- duckvault /absolute/path/to/your/obsidian/vault
```

**By editing the configuration file:**
Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": [
        "/absolute/path/to/your/obsidian/vault"
      ]
    }
  }
}
```

### Gemini CLI
You can add the server persistently using the CLI command (recommended) or by editing the configuration file.

**Using the CLI command:**
```bash
gemini mcp add duckvault --scope user duckvault /absolute/path/to/your/obsidian/vault
```

**By editing the configuration file:**
Add to the `mcpServers` section of your configuration file (`~/.gemini/settings.json`).

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": [
        "/absolute/path/to/your/obsidian/vault"
      ]
    }
  }
}
```

### GitHub Copilot (in the CLI)
GitHub Copilot in the CLI supports MCP servers. You can add the server using the interactive `/mcp` command or by editing the configuration file.

**Using the CLI command:**
1. Start an interactive session: `copilot` (or `gh copilot chat`).
2. Run the `/mcp add` command and follow the prompts.

**By editing the configuration file:**
Add to `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": [
        "/absolute/path/to/your/obsidian/vault"
      ]
    }
  }
}
```
```

## MCP Tools

The server exposes the following tools to AI agents:

1. `search_notes(query: str, tag: Optional[str] = None, limit: int = 5)`: Search for relevant notes using natural language.
2. `list_recent_notes(days: int = 7)`: List notes that were recently updated.

## License

MIT
