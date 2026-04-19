# Functional Specification: DuckVault-MCP

This document serves as the absolute source of truth for the `mcp-duckvault` project. It describes the functional requirements, architectural design, and implementation details of the system.

## 1. System Overview

`mcp-duckvault` is a Model Context Protocol (MCP) server that provides a Retrieval-Augmented Generation (RAG) system for Obsidian Vaults. It indexes Markdown notes into a DuckDB database and enables semantic search via vector embeddings.

### Key Workflows
1.  **Initialization**: CLI initializes the DuckDB schema and loads necessary extensions.
2.  **Indexing**: Performs a full scan of the vault to synchronize files.
3.  **Real-time Updates**: Monitors the vault for file changes and updates the index incrementally.
4.  **Retrieval**: Exposes tools via MCP for AI agents to search and reference notes using vector similarity.

---

## 2. Environment & Tech Stack

-   **Language**: Python 3.11+
-   **Package Manager**: `uv`
-   **Database**: DuckDB (with `vss` extension for vector search and HNSW indexing)
-   **Embedding Model**: `intfloat/multilingual-e5-small` (Sentence-Transformers)
-   **MCP Framework**: FastMCP
-   **File Monitoring**: Watchdog
-   **UI/UX**: `tqdm` for progress indicators
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
    -   Exposing tools (`search_notes`, `list_recent_notes`) to AI agents.
    -   **Obsidian URI Generation**: Provides direct links (`obsidian://open?vault=...`) for all retrieved results.
    -   Executing vector search queries against DuckDB with enhanced tag filtering.

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

---

## 8. Error Handling & Logging

-   **Standard Streams**: `stdout` is reserved for MCP JSON-RPC. All application logs (Info, Error, Debug) MUST go to `stderr`.
-   **Transactions**: Indexing a file is atomic. If chunking or embedding fails, the transaction is rolled back, preserving the previous state of the file in the database.
-   **Database Safety**: Automatic HNSW index creation is wrapped in try-catch to allow fallback to linear search if the extension fails.

