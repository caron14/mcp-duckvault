# DuckVault-MCP

DuckVault-MCP v0.3.0 is a local RAG server for Obsidian Vaults and Markdown
knowledge bases. It combines DuckDB vector search, local GraphRAG, [Open Knowledge
Format (OKF) support](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md), and an offline interactive graph viewer behind an MCP
interface.

- [Quick Start](#quick-start--v030)
- [Features](#features)
- [CLI Reference](#cli-reference)
- [MCP Tools](#mcp-tools)
- [OKF Authoring](#okf-authoring)
- [AI Agent Setup](#ai-agent-setup)
- [Troubleshooting](#troubleshooting)

## Quick Start — v0.3.0

### 1. Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- An Obsidian Vault or directory containing Markdown files
- An absolute path to that directory

### 2. Install

From a local checkout:

```bash
git clone https://github.com/caron14/mcp-duckvault.git
cd mcp-duckvault
uv tool install .
```

Alternatively, install the packaged release:

```bash
uv tool install mcp-duckvault
```

### 3. Run the first sync

Use a dedicated database file for each Vault:

```bash
duckvault /absolute/path/to/vault \
  --db-path ~/.duckvault/my-vault.db \
  --sync-only
```

The first sync may download `intfloat/multilingual-e5-small`. Later starts load
the model from `~/.duckvault/models` and only re-index changed Markdown files.

### 4. Connect Claude Code

```bash
claude mcp add duckvault -- \
  duckvault /absolute/path/to/vault \
  --db-path ~/.duckvault/my-vault.db

claude mcp list
```

The registered command performs an incremental sync before starting the MCP
server. Running the first sync separately avoids a model download during the
initial MCP connection.

### 5. Try the retrieval tools

Ask your AI agent:

- “Find notes about customer revenue.”
- “Find notes tagged `sales` that discuss orders.”
- “Use `find_related_notes` for `tables/orders.md`.”
- “Use `hybrid_search_notes` to find customer metrics and linked notes.”
- “List OKF concepts with type `table` and tag `sales`.”
- “Explain the OKF concept `tables/orders`.”

### 6. Generate the offline graph viewer

```bash
duckvault /absolute/path/to/vault \
  --db-path ~/.duckvault/my-vault.db \
  --visualize \
  --output ./duckvault-graph.html
```

This writes:

```text
duckvault-graph.html
duckvault-graph.json
```

Open the HTML file in a modern browser. It contains the graph data and
Cytoscape.js, requires no backend or network connection, and does not embed the
Markdown bodies.

## Features

| Area | Capability |
| --- | --- |
| Vector RAG | Local E5 embeddings, DuckDB VSS, cosine similarity, optional tag filtering |
| GraphRAG | Markdown links, Wiki links, headings, tags, folders, resources, and citations |
| OKF | Concept detection, type/resource/tag metadata, reserved `index.md` and `log.md` files |
| Hybrid retrieval | Vector seeds expanded through graph relationships and reranked |
| Visualization | Self-contained HTML plus canonical JSON with search, filters, layouts, and diagnostics |
| Indexing | MD5-based incremental sync, `.vaultignore`, filesystem watching |
| Integration | MCP tools with direct Obsidian links |

All indexed content and graph data remain local. Initial setup may download the
DuckDB VSS extension and embedding model. The generated graph viewer makes no
network requests; external resource links are opened only when selected.

## How It Works

1. DuckVault scans Markdown files, excluding configured paths.
2. It parses YAML frontmatter, splits content at H1-H3 headings, and generates
   local embeddings.
3. It extracts document, link, heading, tag, folder, OKF type, resource, and
   citation graph records into DuckDB.
4. MCP tools query the vector index, traverse the graph, or combine both.
5. Visualization mode exports the persisted graph without re-parsing Markdown.

The existing `search_notes` vector behavior remains available alongside the
GraphRAG and OKF tools.

## CLI Reference

```text
duckvault [OPTIONS] VAULT_PATH
```

`VAULT_PATH` must be an existing directory. Absolute paths are strongly
recommended, and are required in AI agent configuration.

| Option | Description |
| --- | --- |
| `--db-path PATH` | DuckDB path. Default: `~/.duckvault/vault.db` |
| `--sync-only` | Sync the Vault and exit without starting MCP |
| `--visualize` | Sync, generate offline HTML/JSON, and exit |
| `--output FILE` | Visualization HTML path. Default: `duckvault-graph.html` |
| `--json-output FILE` | Override the JSON sidecar path |
| `-v`, `--verbose` | Enable debug logging on stderr |

### Common commands

Start the MCP server and filesystem watcher:

```bash
duckvault /absolute/path/to/vault
```

Use a custom database:

```bash
duckvault /absolute/path/to/vault --db-path ./vault-index.db
```

Choose both visualization output paths:

```bash
duckvault /absolute/path/to/vault \
  --visualize \
  --output ./artifacts/graph.html \
  --json-output ./artifacts/graph-data.json
```

## MCP Tools

| Tool | Description |
| --- | --- |
| `search_notes(query, tag=None, limit=5)` | Vector similarity search with optional frontmatter tag filtering |
| `list_recent_notes(days=7)` | Notes indexed within the requested number of days |
| `find_related_notes(path, depth=1, limit=10)` | Related documents found by graph traversal |
| `list_graph_neighbors(path, depth=1, limit=20)` | Neighboring documents and structural graph nodes |
| `hybrid_search_notes(query, tag=None, limit=5, graph_depth=1)` | Vector results reranked with graph relationships |
| `search_okf_concepts(okf_type=None, tag=None, limit=20)` | OKF concepts filtered by type and tag |
| `explain_okf_concept(concept_id)` | Structured OKF metadata, links, resources, citations, and related notes |

Hybrid results use:

```text
final_score = 0.7 * vector_similarity + 0.3 * graph_score
graph_score = max(seed_vector_similarity / depth)
```

Document-oriented retrieval results include `obsidian://open` links.

## Graph Visualization

The viewer initially displays documents and document-to-document relationships.
The sidebar can enable:

- folders
- headings
- tags
- OKF types
- resources and citations
- dangling link targets

It also provides:

- title, path, concept ID, and tag search
- type, tag, directory, relation, and structural-node filters
- force-directed, concentric, breadth-first, circle, and grid layouts
- structured metadata and inbound/outbound relationships
- Obsidian and validated HTTP/HTTPS resource links
- orphan, dangling-link, duplicate-title, and high-degree diagnostics

The HTML enforces a Content Security Policy that prevents external connections.
The JSON sidecar uses schema version `1.0`.

## OKF Authoring

Any Markdown file with leading YAML frontmatter containing `type` is indexed as
an OKF Concept Document.

```markdown
---
type: table
title: Orders
description: One row per customer order
resource: https://console.example.com/warehouse/orders
tags: [sales, finance]
timestamp: 2026-07-01T00:00:00Z
---

# Orders

Each order belongs to a [customer](./customers.md).
Revenue is defined by [weekly revenue](/metrics/weekly_revenue.md).
```

The Concept ID is the Vault-relative path without `.md`:

```text
tables/orders.md -> tables/orders
```

Link resolution:

| Link | Resolution |
| --- | --- |
| `/tables/customers.md` | Vault root |
| `./customers.md` | Current document directory |
| `../metrics/revenue.md` | Relative parent directory |
| `https://example.com/spec` | External citation |
| Missing local target | Retained as a dangling graph node |

`index.md` and `log.md` are stored as navigation/history nodes, not OKF
concepts. Frontmatter fields beyond the standard OKF fields remain available in
node metadata.

### Graph mapping

| Markdown/OKF element | Graph representation |
| --- | --- |
| Markdown file | `document`, `okf_concept`, `okf_index`, or `okf_log` node |
| H1-H3 heading | `heading` node and `HAS_HEADING` edge |
| Frontmatter or inline tag | `tag` node and `HAS_TAG` edge |
| Wiki/Markdown link | `LINKS_TO` or `MENTIONS_LINK` edge |
| Directory | `folder` node and `CONTAINS` edge |
| OKF `type` | `okf_type` node and `HAS_TYPE` edge |
| OKF `resource` | `resource` node and `DESCRIBES_RESOURCE` edge |
| External URL | `citation` node and `CITES_SOURCE` edge |

## AI Agent Setup

Use the same absolute Vault path and database path in every configuration.

### Claude Code

```bash
claude mcp add duckvault -- \
  duckvault /absolute/path/to/vault \
  --db-path /absolute/path/to/duckvault.db
```

### Claude Desktop

Add this entry to the `mcpServers` object in
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "duckvault": {
      "command": "duckvault",
      "args": [
        "/absolute/path/to/vault",
        "--db-path",
        "/absolute/path/to/duckvault.db"
      ]
    }
  }
}
```

### Gemini CLI

```bash
gemini mcp add --scope user duckvault \
  duckvault /absolute/path/to/vault \
  --db-path /absolute/path/to/duckvault.db
```

Verify with:

```bash
gemini mcp list
```

### GitHub Copilot CLI

Start Copilot, run `/mcp add`, and register:

```text
name: duckvault
command: duckvault
args: /absolute/path/to/vault --db-path /absolute/path/to/duckvault.db
```

## Configuration

### Database and model cache

| Data | Default location |
| --- | --- |
| DuckDB index | `~/.duckvault/vault.db` |
| Sentence Transformers cache | `~/.duckvault/models` |

Use a separate `--db-path` for each Vault. Synchronizing an unrelated Vault
against the same database replaces the indexed document set.

### Excluding files

Create `.vaultignore` at the Vault root. Patterns use glob matching.
`.obsidian` and `.trash` are always excluded.

```text
# Directories
private/
drafts/

# File patterns
archive-*.md
```

## Troubleshooting

### The first start is slow

The embedding model is loaded lazily and tried from the local cache first. If it
is not cached, DuckVault downloads it from Hugging Face. Run `--sync-only`
before registering MCP to complete this step separately.

### MCP fails to connect

- Confirm `duckvault` is available in the same environment as the AI agent.
- Use absolute Vault and database paths.
- Run the configured command manually with `--sync-only`.
- Add `--verbose`; logs are written to stderr so MCP stdout remains valid.

### Notes or graph records look stale

DuckVault normally re-indexes files when their MD5 changes. If parsing, schema,
or embedding configuration changed between versions, remove the affected
database and run a full sync again:

```bash
rm ~/.duckvault/my-vault.db
duckvault /absolute/path/to/vault \
  --db-path ~/.duckvault/my-vault.db \
  --sync-only
```

Back up the database first if it is needed for diagnostics.

### The graph viewer is empty

- Confirm the sync completed successfully.
- Check that Markdown files are not excluded by `.vaultignore`.
- Verify that HTML and JSON output paths differ.
- Inspect the JSON sidecar to confirm `stats.documents` is greater than zero.

### Broken links appear in diagnostics

This is expected for unresolved local links. DuckVault preserves them as
`link_target` nodes so they can be found and repaired.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run black --check src tests
uv run isort --check-only src tests
```

The functional source of truth is [SPECIFICATION.md](SPECIFICATION.md).

## Current Scope

v0.3.0 does not include LLM entity extraction, community detection, community
summaries, Global GraphRAG, Neo4j export, GraphML/CSV export, or CI-enforced
graph quality gates.

## License

DuckVault-MCP is released under the MIT License. The bundled Cytoscape.js
library is also MIT-licensed; its license text is included at
`src/mcp_duckvault/assets/CYTOSCAPE_LICENSE.txt`.
