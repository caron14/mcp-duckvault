# DuckVault-MCP

[![PyPI version](https://img.shields.io/pypi/v/mcp-duckvault)](https://pypi.org/project/mcp-duckvault/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-duckvault)](https://pypi.org/project/mcp-duckvault/)
[![License: MIT](https://img.shields.io/github/license/caron14/mcp-duckvault)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/mcp-duckvault)](https://pypi.org/project/mcp-duckvault/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

DuckVault-MCP v0.4.0 is a local vector and graph RAG server for Obsidian Vaults
and Markdown knowledge bases. Each Vault receives an isolated DuckDB index, and
all MCP sessions for that Vault share one local daemon so DuckDB, WAL, VSS, the
watcher, and the embedding model have a single owner.

## Quick start

Requirements:

- Python 3.11–3.14
- macOS, Linux, or Windows
- an existing directory containing Markdown files
- network access during the first `init` only

Install and initialize:

```bash
uv tool install mcp-duckvault
duckvault init /absolute/path/to/vault
```

`init` installs the DuckDB VSS extension, downloads
`intfloat/multilingual-e5-small`, creates a Vault-specific database, performs the
first sync and an offline search smoke test, then writes a portable MCP server
entry. Its location is printed as `mcp_config`.

The generated entry is equivalent to:

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": ["serve", "/absolute/path/to/vault"]
    }
  }
}
```

After initialization, normal MCP startup and search use cached assets only and
do not install extensions or download models.

## CLI

v0.4 uses explicit subcommands. The pre-v0.4 form `duckvault VAULT_PATH` is no
longer accepted.

```text
duckvault init VAULT_PATH [--non-interactive] [--json]
duckvault serve VAULT_PATH
duckvault sync VAULT_PATH [--json]
duckvault sync VAULT_PATH --dry-run [--json]
duckvault status VAULT_PATH [--json]
duckvault doctor VAULT_PATH [--json]
duckvault daemon start|stop|restart|status VAULT_PATH
duckvault migrate-legacy VAULT_PATH [--legacy-db PATH]
duckvault reindex VAULT_PATH [--json]
duckvault explain-ignore VAULT_PATH PATH [--json]
duckvault visualize VAULT_PATH [--output FILE]
duckvault --version
```

Normally no database path is needed. DuckVault derives one from the normalized
Vault path:

```text
~/.duckvault/
├── models/
└── vaults/<vault-id>/
    ├── vault.db
    ├── endpoint.json
    ├── owner.lock
    ├── startup.lock
    ├── daemon.log
    ├── mcp-server.json
    └── backups/
```

An advanced `--db-path` override remains available. DuckVault stores the Vault
identity in every database and refuses a mismatched database before indexing or
deleting anything.

### Status and synchronization failures

```bash
duckvault sync /absolute/path/to/vault --json
duckvault status /absolute/path/to/vault --json
```

A sync reports `scanned`, `indexed`, `skipped`, `deleted`, `failed`, and
`excluded`. Exit status is `0` for complete, `2` for partial success, and `1`
for failure. Current file failures are retained with a stable error code and
timestamp and are cleared after a successful retry. Note bodies are never
written to logs or failure records.

### Diagnostics

```bash
duckvault doctor /absolute/path/to/vault
duckvault doctor /absolute/path/to/vault --json
```

`doctor` checks Python, DuckDB VSS, the model cache, the database, Vault
permissions, Vault identity, daemon health, watcher ownership, and offline
readiness. Failed checks include a concrete repair command.

### Legacy database migration

Pre-v0.4 used the shared `~/.duckvault/vault.db`, which has no reliable Vault
identity. DuckVault therefore does not copy its index into a new Vault:

```bash
duckvault migrate-legacy /absolute/path/to/vault
```

The command checkpoints and backs up the legacy DB, leaves the source intact,
and rebuilds a new Vault-specific index with the current parser, graph extractor,
and embedding configuration. Do not delete the old DB until the new status and
search results have been verified.

Versioned schema migrations create a checkpointed backup in `backups/` before
running in a transaction. If the parser, model, or embedding dimension changes,
rebuild the index while the daemon is stopped:

```bash
duckvault daemon stop /absolute/path/to/vault
duckvault reindex /absolute/path/to/vault --json
```

The replacement is built in a separate database and installed only after a
complete sync. A failed rebuild leaves the original database and backup intact;
`status` and `doctor` report the recovery command.

## Shared daemon and recovery

`duckvault serve` is a small stdio MCP proxy. It connects to an authenticated
loopback endpoint and starts the Vault daemon if needed. Kernel-backed owner and
startup locks ensure that concurrent MCP sessions still create only one owner.
All database work is serialized through that daemon.

The daemon exposes these states: `starting`, `preparing`, `syncing`, `ready`,
`degraded`, `reindex_required`, and `stopping`. Health and status remain
available while preparation or synchronization is running.

Useful recovery commands:

```bash
duckvault daemon status /absolute/path/to/vault
duckvault daemon restart /absolute/path/to/vault
duckvault doctor /absolute/path/to/vault
```

After SIGTERM or Ctrl+C, the daemon drains queued work, checkpoints and closes
DuckDB, and removes its endpoint. After an unclean exit, kernel locks are
released by the OS and the next proxy replaces stale endpoint metadata.

## MCP tools

| Tool | Description |
| --- | --- |
| `search_notes(query, tag=None, limit=5)` | Vector similarity search |
| `list_recent_notes(days=7, limit=20)` | Notes recently modified on disk |
| `find_related_notes(path, depth=1, limit=10)` | Related graph documents |
| `list_graph_neighbors(path, depth=1, limit=20)` | Neighboring graph nodes |
| `hybrid_search_notes(query, tag=None, limit=5, graph_depth=1)` | Vector plus graph retrieval |
| `search_okf_concepts(okf_type=None, tag=None, limit=20)` | OKF concept search |
| `explain_okf_concept(concept_id)` | OKF metadata and relationships |
| `get_index_status(include_failures=False, failure_limit=100)` | Readiness, completeness, and failures |

Search results include `obsidian://open` links. Markdown frontmatter, H1–H3
headings, Markdown/Wiki links, tags, folders, resources, citations, and OKF
concept metadata are represented in the local graph.

All MCP tools return versioned structured data. Retrieval responses contain
`schema_version`, `tool`, `count`, and `items`; errors expose a stable `code`,
`message`, and `retryable` flag. Limits are bounded to 100 results, graph depth
to 5, and snippets to 2,000 characters.

## Exclusions and visualization

`.obsidian` and `.trash` are excluded by default. Add patterns to
`VAULT_PATH/.vaultignore`, one per line. Current matching supports simple glob
patterns but is not fully gitignore-compatible.

Preview a sync without loading the model or changing the database, and inspect
why a path is excluded:

```bash
duckvault sync /absolute/path/to/vault --dry-run --json
duckvault explain-ignore /absolute/path/to/vault private/note.md --json
```

Markdown files larger than 10 MiB fail safely without replacing their previous
index entry. Configure the limit with `--max-file-size BYTES` or
`DUCKVAULT_MAX_MARKDOWN_BYTES`. File and directory symlinks are not followed;
Vault-external targets are never indexed.

Stop the daemon before reading the DB for a graph export:

```bash
duckvault daemon stop /absolute/path/to/vault
duckvault visualize /absolute/path/to/vault --output duckvault-graph.html
```

The HTML viewer is self-contained, makes no network requests, and does not embed
Markdown bodies. A versioned JSON sidecar is generated beside it.

## Privacy, backup, and upgrades

- Vault contents, chunks, embeddings, graph data, and metadata are stored in
  plaintext DuckDB files. Anyone who can read the database can inspect them.
- DuckVault applies private POSIX permissions (`0700` directories and `0600`
  databases/configuration) where supported. Windows ACLs remain controlled by
  the user account and parent directory.
- Back up the Vault and its `~/.duckvault/vaults/<vault-id>/` directory together.
- Schema and index configuration versions are stored in `system_config`. A
  changed parser/model signature marks the index for rebuilding instead of
  silently reusing incompatible embeddings.
- Legacy migration creates a checkpointed backup before rebuilding. A database
  with a newer unsupported schema is rejected rather than modified.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run black --check src tests .github/scripts
uv run isort --check-only src tests .github/scripts
```

CI runs pytest on Python 3.11–3.14 on Linux and representative macOS/Windows
versions, plus a clean wheel CLI smoke test.

## License

MIT. The offline viewer bundles Cytoscape.js under its included license.
