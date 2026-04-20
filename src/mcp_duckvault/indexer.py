import fnmatch
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import yaml
from sentence_transformers import SentenceTransformer
from tqdm import tqdm
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .db_manager import DatabaseManager

logger = logging.getLogger(__name__)


def _json_default(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is NOT JSON serializable")


class EmbeddingModel:
    """Wrapper for the sentence-transformers model to generate text embeddings.

    This class handles the loading of the embedding model and provides a
    consistent interface for encoding text into vectors with the appropriate
    prefixes required by the E5 model family.

    Attributes:
        model (SentenceTransformer): The underlying sentence-transformers model.
        model_name (str): The name/ID of the model being used.
    """

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small"):
        """Initializes the EmbeddingModel with a specific pre-trained model.

        Args:
            model_name (str): The name of the model to load from HuggingFace
                or a local path. Defaults to "intfloat/multilingual-e5-small".
        """
        self.model_name = model_name
        self._model = None
        # Default dimension for the e5-small model family to allow lazy loading
        self._dimension = (
            384 if model_name == "intfloat/multilingual-e5-small" else None
        )

    @property
    def model(self) -> SentenceTransformer:
        """Lazily loads and returns the SentenceTransformer model."""
        if self._model is None:
            logger.info(f"Loading embedding model: {self.model_name}")
            try:
                # First attempt: load locally to avoid slow HuggingFace network checks
                # Set environment variable to strictly enforce offline mode
                os.environ["HF_HUB_OFFLINE"] = "1"
                self._model = SentenceTransformer(self.model_name, local_files_only=True)
                logger.info(f"Loaded {self.model_name} from local cache.")
            except Exception:
                # Fallback: download if not present
                os.environ["HF_HUB_OFFLINE"] = "0"
                logger.info(
                    f"Model not found locally. Downloading {self.model_name} from HuggingFace..."
                )
                self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def dimension(self) -> int:
        """Gets the embedding dimension of the model.

        Returns:
            int: The size of the vector produced by the model.
        """
        if self._dimension:
            return self._dimension
        return self.model.get_embedding_dimension()

    def encode(self, texts: List[str], is_query: bool = False) -> List[List[float]]:
        """Encodes a list of strings into a list of vector embeddings.

        Adds 'query: ' prefix for search queries or 'passage: ' prefix for
        documents being indexed, as required by E5-style models.

        Args:
            texts (List[str]): A list of text strings to encode.
            is_query (bool): Whether the text is a search query. If False,
                it's treated as a passage for indexing. Defaults to False.

        Returns:
            List[List[float]]: A list of embeddings, where each embedding
                is a list of floats.
        """
        prefix = "query: " if is_query else "passage: "
        prefixed_texts = [prefix + text for text in texts]
        embeddings = self.model.encode(prefixed_texts)
        return embeddings.tolist()


class MarkdownParser:
    """Utility class for parsing Markdown content.

    Provides static methods for extracting metadata (YAML frontmatter) and
    splitting content into logical chunks based on headers.
    """

    @staticmethod
    def extract_metadata(content: str) -> tuple[Dict[str, Any], str]:
        """Extracts YAML frontmatter and returns it along with the remaining body.

        Args:
            content (str): The full content of a Markdown file.

        Returns:
            tuple[Dict[str, Any], str]: A tuple containing:
                - A dictionary of extracted metadata.
                - The remaining content after the frontmatter.
        """
        frontmatter = {}
        remaining_content = content

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if match:
            try:
                frontmatter = yaml.safe_load(match.group(1)) or {}
                remaining_content = content[match.end() :]
            except Exception as e:
                logger.warning(f"Failed to parse YAML frontmatter: {e}")

        return frontmatter, remaining_content

    @staticmethod
    def chunk_by_headers(content: str) -> List[str]:
        """Chunks Markdown content based on H1, H2, or H3 headers.

        Each chunk starts with a header and includes all text until the
        next header of the same or higher level is encountered.

        Args:
            content (str): The Markdown body content to chunk.

        Returns:
            List[str]: A list of text chunks.
        """
        # Split by H1-H3: lines starting with #, ##, or ###
        # We keep the header in the chunk
        chunks = []
        lines = content.split("\n")
        current_chunk = []

        header_pattern = re.compile(r"^#{1,3}\s+")

        for line in lines:
            if header_pattern.match(line) and current_chunk:
                # New header found, save current chunk
                chunks.append("\n".join(current_chunk).strip())
                current_chunk = [line]
            else:
                current_chunk.append(line)

        if current_chunk:
            chunks.append("\n".join(current_chunk).strip())

        # Filter out empty chunks
        return [c for c in chunks if c]


class VaultIndexer:
    """Orchestrates the indexing of an Obsidian Vault into DuckDB.

    This class handles file discovery, change detection using MD5 hashes,
    parsing, embedding generation, and database updates for a vault.

    Attributes:
        vault_path (str): Absolute path to the Obsidian Vault.
        db (DatabaseManager): The database manager instance.
        model (EmbeddingModel): The embedding model instance.
        parser (MarkdownParser): The Markdown parser instance.
        exclude_patterns (List[str]): List of glob patterns to exclude from indexing.
    """

    def __init__(
        self, vault_path: str, db_manager: DatabaseManager, model: Optional[EmbeddingModel] = None
    ):
        """Initializes the VaultIndexer.

        Args:
            vault_path (str): The path to the Obsidian Vault.
            db_manager (DatabaseManager): An initialized DatabaseManager.
            model (Optional[EmbeddingModel]): An EmbeddingModel instance.
                If not provided, a default one will be created.
        """
        self.vault_path = os.path.abspath(vault_path)
        self.db = db_manager
        self.model = model or EmbeddingModel()
        self.parser = MarkdownParser()
        self.exclude_patterns = self._load_exclude_patterns()

    def _load_exclude_patterns(self) -> List[str]:
        """Loads exclusion patterns from .vaultignore or uses defaults.

        Returns:
            List[str]: A list of glob patterns to ignore.
        """
        ignore_file = os.path.join(self.vault_path, ".vaultignore")
        patterns = [".obsidian", ".trash"]  # Default exclusions

        if os.path.exists(ignore_file):
            try:
                with open(ignore_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            patterns.append(line)
                logger.info(f"Loaded {len(patterns) - 2} patterns from .vaultignore")
            except Exception as e:
                logger.error(f"Failed to load .vaultignore: {e}")

        return list(set(patterns))

    def _is_excluded(self, rel_path: str) -> bool:
        """Checks if a given relative path matches any exclusion patterns.

        Args:
            rel_path (str): The relative path of the file or directory.

        Returns:
            bool: True if the path should be excluded, False otherwise.
        """
        # Normalize slashes for matching
        norm_path = rel_path.replace(os.sep, "/")
        path_parts = norm_path.split("/")

        for pattern in self.exclude_patterns:
            # Handle patterns like "private/" by stripping trailing slash
            clean_pattern = pattern.replace(os.sep, "/").rstrip("/")

            # 1. Match against full relative path
            if fnmatch.fnmatch(norm_path, clean_pattern) or fnmatch.fnmatch(
                norm_path, f"{clean_pattern}/*"
            ):
                return True

            # 2. Match against each part of the path (for simple patterns like ".obsidian")
            for part in path_parts:
                if fnmatch.fnmatch(part, clean_pattern):
                    return True
        return False

    def get_file_hash(self, file_path: str) -> str:
        """Calculates the MD5 hash of a file's content.

        Args:
            file_path (str): The full path to the file.

        Returns:
            str: The MD5 hex digest.
        """
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            buf = f.read()
            hasher.update(buf)
        return hasher.hexdigest()

    def index_file(self, file_path: str, show_log: bool = True):
        """Indexes a single Markdown file into the database.

        Checks if the file is a Markdown file, if it's excluded, and if
        it has changed since the last indexing. If changed, it parses
        the file, generates embeddings for its chunks, and updates the database.

        Args:
            file_path (str): The full path to the Markdown file.
            show_log (bool): Whether to log the indexing progress. Defaults to True.
        """
        if not file_path.endswith(".md"):
            return

        rel_path = os.path.relpath(file_path, self.vault_path)
        if self._is_excluded(rel_path):
            return

        if show_log:
            logger.info(f"Indexing file: {rel_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

            # Check if file has changed
            existing = self.db.conn.execute(
                "SELECT md5 FROM documents WHERE path = ?", (rel_path,)
            ).fetchone()

            if existing and existing[0] == file_hash:
                if show_log:
                    logger.debug(f"File unchanged: {rel_path}")
                return

            # File changed or new, proceed to index
            metadata, body = self.parser.extract_metadata(content)
            chunks = self.parser.chunk_by_headers(body)

            # Use a transaction for the update
            self.db.conn.execute("BEGIN TRANSACTION")
            try:
                # 1. Update/Insert document (Manually handle cleanup)
                self.db.conn.execute("DELETE FROM chunks WHERE document_path = ?", (rel_path,))
                self.db.conn.execute("DELETE FROM documents WHERE path = ?", (rel_path,))
                self.db.conn.execute(
                    "INSERT INTO documents (path, md5, metadata) VALUES (?, ?, ?)",
                    (rel_path, file_hash, json.dumps(metadata, default=_json_default)),
                )

                # 2. Generate embeddings for chunks and insert
                if chunks:
                    embeddings = self.model.encode(chunks)
                    for i, (chunk_text, vec) in enumerate(zip(chunks, embeddings)):
                        chunk_id = str(uuid.uuid4())
                        self.db.conn.execute(
                            "INSERT INTO chunks (chunk_id, document_path, content, embedding, metadata) VALUES (?, ?, ?, ?, ?)",
                            (chunk_id, rel_path, chunk_text, vec, json.dumps({"index": i}, default=_json_default)),
                        )

                self.db.conn.execute("COMMIT")
                if show_log:
                    logger.info(f"Successfully indexed {rel_path} ({len(chunks)} chunks)")
            except Exception as e:
                self.db.conn.execute("ROLLBACK")
                logger.error(f"Error during transaction for {rel_path}: {e}")

        except Exception as e:
            logger.error(f"Failed to index file {file_path}: {e}")

    def delete_file(self, file_path: str):
        """Removes a file and its chunks from the index.

        Args:
            file_path (str): The full path to the file to be removed.
        """
        rel_path = os.path.relpath(file_path, self.vault_path)
        if self._is_excluded(rel_path):
            return

        logger.info(f"Deleting file from index: {rel_path}")
        self.db.conn.execute("DELETE FROM chunks WHERE document_path = ?", (rel_path,))
        self.db.conn.execute("DELETE FROM documents WHERE path = ?", (rel_path,))

    def full_sync(self):
        """Performs a full synchronization of the vault.

        Scans all Markdown files in the vault, indexes new or modified ones,
        and removes entries for files that no longer exist on disk.
        """
        logger.info("Starting full sync...")

        # Get all files in DB to find deletions
        db_files = set(
            row[0] for row in self.db.conn.execute("SELECT path FROM documents").fetchall()
        )
        current_files = []

        # First pass: collect all files to index
        for root, dirs, files in os.walk(self.vault_path):
            # filter dirs in-place to avoid traversing excluded directories
            dirs[:] = [
                d
                for d in dirs
                if not self._is_excluded(os.path.relpath(os.path.join(root, d), self.vault_path))
            ]

            for file in files:
                if file.endswith(".md"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.vault_path)
                    if not self._is_excluded(rel_path):
                        current_files.append((full_path, rel_path))

        # Second pass: index files with progress bar
        if current_files:
            logger.info(f"Found {len(current_files)} markdown files. Syncing...")
            for full_path, rel_path in tqdm(current_files, desc="Syncing Vault", unit="file"):
                self.index_file(full_path, show_log=False)

        # Remove files that no longer exist
        current_rel_paths = set(p for _, p in current_files)
        deleted_files = db_files - current_rel_paths
        for rel_path in deleted_files:
            logger.info(f"Removing deleted file: {rel_path}")
            self.db.conn.execute("DELETE FROM chunks WHERE document_path = ?", (rel_path,))
            self.db.conn.execute("DELETE FROM documents WHERE path = ?", (rel_path,))

        logger.info("Full sync complete")


class VaultWatchdogHandler(FileSystemEventHandler):
    """Event handler for monitoring filesystem changes in the vault.

    Dispatches modified, created, deleted, and moved events to the
    VaultIndexer for real-time incremental updates.

    Attributes:
        indexer (VaultIndexer): The indexer instance to handle changes.
    """

    def __init__(self, indexer: VaultIndexer):
        """Initializes the VaultWatchdogHandler.

        Args:
            indexer (VaultIndexer): The indexer to use for incremental updates.
        """
        self.indexer = indexer

    def on_modified(self, event):
        """Called when a file or directory is modified."""
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.index_file(event.src_path)

    def on_created(self, event):
        """Called when a file or directory is created."""
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.index_file(event.src_path)

    def on_deleted(self, event):
        """Called when a file or directory is deleted."""
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.delete_file(event.src_path)

    def on_moved(self, event):
        """Called when a file or directory is moved or renamed."""
        if not event.is_directory:
            if event.src_path.endswith(".md"):
                self.indexer.delete_file(event.src_path)
            if event.dest_path.endswith(".md"):
                self.indexer.index_file(event.dest_path)


def start_watcher(vault_path: str, indexer: VaultIndexer):
    """Starts the watchdog observer to monitor the vault for changes.

    Args:
        vault_path (str): The path to the vault to monitor.
        indexer (VaultIndexer): The indexer to handle filesystem events.

    Returns:
        Observer: The started watchdog observer instance.
    """
    event_handler = VaultWatchdogHandler(indexer)
    observer = Observer()
    observer.schedule(event_handler, vault_path, recursive=True)
    observer.start()
    logger.info(f"Started monitoring vault at: {vault_path}")
    return observer
