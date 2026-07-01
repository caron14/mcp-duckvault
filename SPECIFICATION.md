# Functional Specification: DuckVault-MCP

This document serves as the absolute source of truth for the `mcp-duckvault` project. It describes the functional requirements, architectural design, and implementation details of the system.

## 1. System Overview

`mcp-duckvault` is a Model Context Protocol (MCP) server that provides local
Retrieval-Augmented Generation for Obsidian Vaults. It combines semantic vector
search with a Markdown and Open Knowledge Format (OKF) graph stored in DuckDB.

### Key Workflows
1.  **Initialization**: CLI initializes the DuckDB schema and loads necessary extensions.
2.  **Indexing**: Performs a full scan of the vault to synchronize files.
3.  **Real-time Updates**: Monitors the vault for file changes and updates the index incrementally.
4.  **Retrieval**: Exposes vector, graph, hybrid, and OKF tools through MCP.
5.  **Visualization**: Exports the synchronized graph as offline HTML and canonical JSON.

---

## 2. Environment & Tech Stack

-   **Language**: Python 3.11+
-   **Package Manager**: `uv`
-   **Database**: DuckDB (with `vss` extension for vector search and HNSW indexing)
-   **Embedding Model**: `intfloat/multilingual-e5-small` (Sentence-Transformers)
-   **MCP Framework**: FastMCP
-   **File Monitoring**: Watchdog
-   **UI/UX**: `tqdm` for progress indicators
-   **Graph UI**: Cytoscape.js 3.34.0, bundled locally under the MIT license
-   **Testing**: `pytest`

---

## 3. Development Standards

### 3.1 Code Formatting and Linting
To maintain code quality and consistency, the following tools are used:
- **Black**: Used for consistent code formatting.
- **isort**: Used for consistent import sorting.

These tools are enforced via GitHub Actions on every push and pull request.

### 3.2 Testing Strategy
- **Unit Tests**: The project uses `pytest` to verify core components.
- **Scope**: Tests cover markdown parsing, header-based chunking, exclusion rules, and search/tag filtering logic.

### 3.3 Dependency Management
- **uv**: The project uses `uv` for dependency management and environment isolation.

---

## 4. Core Components

### 4.1 CLI (`cli.py`)
-   **Role**: Entry point and orchestrator.
-   **Responsibilities**:
    -   Argument parsing (vault path, DB path).
    -   Default path handling: Sets default database and model cache to `~/.duckvault/`.
    -   Shared Component Initialization: Initializes the `EmbeddingModel` instance to be shared across the system.
    -   Logging configuration (all logs to `stderr`).
    -   Lifecycle management: Initialize DB -> Full Sync -> Start Watcher -> Start MCP Server.
    -   Visualization mode: Initialize DB -> Full Sync -> Write HTML/JSON -> Exit.

### 4.2 Database Manager (`db_manager.py`)
-   **Role**: Persistence layer.
-   **Responsibilities**:
    -   DuckDB connection management.
    -   `vss` extension loading.
    -   Schema creation and maintenance.
    -   HNSW index creation on the embedding column.

### 4.3 Indexer (`indexer.py`)
-   **Role**: Data processing and synchronization.
-   **Responsibilities**:
    -   **Markdown Parsing**: Frontmatter extraction and H1-H3 header-based chunking.
    -   **Embedding Generation**: Transforming text chunks into vectors using a shared `EmbeddingModel`.
    -   **Incremental Sync**: MD5 hashing to detect file changes.
    -   **Progress Tracking**: Uses `tqdm` to provide visual feedback during full synchronization.
    -   **Exclusion**: Respects `.vaultignore` and default system exclusions (`.obsidian`, `.trash`).

### 4.4 MCP Server (`mcp_server.py`)
-   **Role**: API Interface.
-   **Responsibilities**:
    -   Exposing vector, graph, hybrid, and OKF retrieval tools to AI agents.
    -   **Obsidian URI Generation**: Provides direct links (`obsidian://open?vault=...`) for all retrieved results.
    -   Executing vector search queries against DuckDB with enhanced tag filtering.

### 4.5 Graph Extractor (`graph_extractor.py`)
-   Extracts H1-H3 headings, Wiki links, Markdown links, frontmatter/inline tags, and folder hierarchy.
-   Classifies OKF Concept Documents and reserved `index.md` / `log.md` files.
-   Resolves Vault-root and document-relative links while retaining broken links.
-   Maps OKF type, resource, title, description, timestamp, tags, and arbitrary metadata.

### 4.6 Graph Repository (`graph_repository.py`)
-   Persists graph records with deterministic IDs.
-   Deletes document-owned graph records atomically and garbage-collects unreferenced shared nodes.
-   Performs stable breadth-first traversal in Python.
-   Retrieves and filters structured OKF concepts.
-   Returns deterministic full-graph snapshots for downstream consumers.

### 4.7 Graph Visualizer (`graph_visualizer.py`)

-   Derives display attributes, degree metrics, and quality diagnostics from the persisted graph.
-   Writes a versioned canonical JSON representation.
-   Generates a self-contained HTML viewer with bundled Cytoscape.js.
-   Uses atomic output replacement and escapes embedded JSON against script injection.

---

## 5. Data Model & Schema

### 5.1 `documents` Table
Stores high-level file metadata.
-   `path` (VARCHAR, PK): Relative path from vault root.
-   `md5` (VARCHAR): File content hash.
-   `metadata` (JSON): Extracted frontmatter.
-   `updated_at` (TIMESTAMP): Last indexing time.

### 5.2 `chunks` Table
Stores individual text fragments and their embeddings.
-   `chunk_id` (VARCHAR, PK): Unique identifier.
-   `document_path` (VARCHAR, FK): Reference to `documents.path`.
-   `content` (TEXT): The actual text content.
-   `embedding` (FLOAT[]): Vector representation.
-   `metadata` (JSON): Chunk-specific info (e.g., chunk index).

### 5.3 Vector Index
-   **Type**: HNSW (Hierarchical Navigable Small World).
-   **Metric**: Cosine Similarity.
-   **Target**: `chunks.embedding`.

### 5.4 Graph Tables

-   `nodes(node_id PK, node_type, name, document_path, metadata, created_at, updated_at)`
-   `edges(edge_id PK, source_node_id, target_node_id, edge_type, weight, document_path, metadata, created_at)`
-   `node_mentions(mention_id PK, node_id, chunk_id, document_path, mention_text, metadata, created_at)`

Document-like nodes use `doc:<relative-path>`. Other IDs use the `heading:`,
`tag:`, `folder:`, `okf_type:`, `resource:`, `citation:`, or `link_target:`
prefix. Graph lookup indexes cover edge endpoints, document ownership, and mentions.

---

## 6. Indexing Specifications

### 6.1 Content Parsing
-   **Frontmatter**: Extracts YAML between `---` markers at the start of the file.
-   **Chunking**: Splits by H1 (`#`), H2 (`##`), or H3 (`###`) headers.
-   **Semantic Integrity**: The header line is included at the beginning of its respective chunk.

### 6.2 Embedding Requirements
Using the E5 model family requirements:
-   **Indexing (Passage)**: Prefixes every chunk with `passage: `.
-   **Retrieval (Query)**: Prefixes search queries with `query: `.

### 6.3 Incremental Sync Logic
-   **Full Sync**: Compares all local `.md` files against the DB. Files missing from disk are deleted from DB. New/modified files are re-indexed.
-   **Real-time**: `watchdog` triggers `index_file` on `modified` or `created` events, and `delete_file` on `deleted` events. `moved` events are handled as a delete/index pair.

### 6.4 Exclusion Rules
-   Always ignores: `.obsidian/`, `.trash/`.
-   Supports `.vaultignore` at the vault root using glob patterns.

### 6.5 Graph and OKF Indexing

-   Each Markdown file has exactly one document-like node.
-   A file with leading YAML frontmatter containing `type` is an `okf_concept`.
-   `index.md` and `log.md` are `okf_index` and `okf_log`, never concepts.
-   An OKF Concept ID is its Vault-relative path without `.md`.
-   Frontmatter and graph extraction, like embedding, occur before the write transaction.
-   File updates atomically replace owned nodes, edges, mentions, documents, and chunks.
-   Shared nodes are garbage-collected after commit.

---

## 7. MCP Tool Specifications

### 7.1 `search_notes(query: str, tag: Optional[str], limit: int)`
-   **Process**:
    1.  Encodes `query` with `query: ` prefix.
    2.  Executes SQL: `1 - (embedding <=> ?::FLOAT[])` for cosine similarity.
    3.  Filters by optional `tag` within the document metadata, supporting multiple JSON formats (arrays, strings, space-separated).
-   **Output**: Markdown formatted string containing:
    -   File paths and similarity scores.
    -   Obsidian URI link (`obsidian://open?vault=...`).
    -   Chunk content.

### 7.2 `list_recent_notes(days: int)`
-   **Process**:
    1.  Queries `documents` table where `updated_at >= CURRENT_TIMESTAMP - (INTERVAL '1 day' * ?)`.
-   **Output**: List of file paths, last update timestamps, and Obsidian URI links.

### 7.3 Graph and OKF Tools

-   `find_related_notes(path, depth=1, limit=10)`: returns related document paths,
    relation types, depth, weight/score, reason, and Obsidian URI.
-   `list_graph_neighbors(path, depth=1, limit=20)`: returns neighboring node types,
    names, relationships, depth, and metadata.
-   `hybrid_search_notes(query, tag=None, limit=5, graph_depth=1)`: expands vector
    result documents through graph traversal. It uses
    `final_score = 0.7 * vector_similarity + 0.3 * graph_score`, where
    `graph_score = max(seed_vector_similarity / depth)`.
-   `search_okf_concepts(okf_type=None, tag=None, limit=20)`: filters concepts by
    OKF type and tag.
-   `explain_okf_concept(concept_id)`: returns concept metadata, resources,
    citations, tags, headings, links, and related notes.

Traversal is breadth-first in Python. Depth 1 is the primary supported case;
larger positive depths are supported.

---

## 8. Error Handling & Logging

-   **Standard Streams**: `stdout` is reserved for MCP JSON-RPC. All application logs (Info, Error, Debug) MUST go to `stderr`.
-   **Transactions**: Indexing a file is atomic. If chunking or embedding fails, the transaction is rolled back, preserving the previous state of the file in the database.
-   **Database Safety**: Automatic HNSW index creation is wrapped in try-catch to allow fallback to linear search if the extension fails.
-   **Visualization Safety**: Metadata is embedded as escaped JSON and rendered with
    DOM text nodes. The generated page denies external connections through its
    Content Security Policy.

---

## 9. Graph Visualization

`duckvault VAULT_PATH --visualize` performs a normal full sync, writes an HTML
viewer and JSON sidecar, then exits without starting the watcher or MCP server.

### 9.1 Canonical JSON

-   Schema version: `1.0`
-   Top-level fields: `schema_version`, `generated_at`, `vault`, `stats`,
    `nodes`, `edges`, `diagnostics`
-   Nodes include persisted identity and metadata plus type, tags, directory,
    degree, color, Obsidian URI, and initial visibility.
-   Edges include source, target, relation, weight, metadata, renderability,
    color, and initial visibility.
-   Diagnostics include orphan documents, dangling links, duplicate titles,
    high-degree documents, and large-graph warnings.

### 9.2 Viewer Behavior

-   Document-like nodes and document relationships are visible initially.
-   Folder, heading, tag, OKF type, resource, citation, and dangling-target
    groups can be enabled independently.
-   Search covers title, path, concept ID, and tags.
-   Filters cover OKF type, tag, directory, relation, and structural node group.
-   Built-in layouts are CoSE, concentric, breadth-first, circle, and grid.
-   The detail panel shows structured metadata and inbound/outbound relations,
    but does not embed Markdown bodies.
-   No backend, CDN, external graph database, or browser network access is required.

---

## 10. GraphRAG Scope

GraphRAG v1 remains fully local and preserves the existing vector search behavior.
It does not include LLM entity extraction, community detection, community
summaries, Global GraphRAG, or an external graph database. Potential future work
includes Entity GraphRAG, Global GraphRAG, community summaries, GraphML/CSV
exports, and CI-enforced graph quality gates.
