import os
import hashlib
import logging
import yaml
import re
import uuid
import json
import fnmatch
from typing import List, Dict, Any, Optional
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from sentence_transformers import SentenceTransformer
from .db_manager import DatabaseManager

logger = logging.getLogger(__name__)

class EmbeddingModel:
    def __init__(self, model_name: str = "intfloat/multilingual-e5-small"):
        logger.info(f"Loading embedding model: {model_name}")
        self.model = SentenceTransformer(model_name)
        self.model_name = model_name

    def encode(self, texts: List[str], is_query: bool = False) -> List[List[float]]:
        """Encode a list of texts into embeddings.
        Adds 'query: ' or 'passage: ' prefix as required by e5 models.
        """
        prefix = "query: " if is_query else "passage: "
        prefixed_texts = [prefix + text for text in texts]
        embeddings = self.model.encode(prefixed_texts)
        return embeddings.tolist()

class MarkdownParser:
    @staticmethod
    def extract_metadata(content: str) -> tuple[Dict[str, Any], str]:
        """Extract YAML frontmatter and the remaining content."""
        frontmatter = {}
        remaining_content = content
        
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if match:
            try:
                frontmatter = yaml.safe_load(match.group(1)) or {}
                remaining_content = content[match.end():]
            except Exception as e:
                logger.warning(f"Failed to parse YAML frontmatter: {e}")
                
        return frontmatter, remaining_content

    @staticmethod
    def chunk_by_headers(content: str) -> List[str]:
        """Chunk markdown content based on H1-H3 headers."""
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
    def __init__(self, vault_path: str, db_manager: DatabaseManager):
        self.vault_path = os.path.abspath(vault_path)
        self.db = db_manager
        self.model = EmbeddingModel()
        self.parser = MarkdownParser()
        self.exclude_patterns = self._load_exclude_patterns()

    def _load_exclude_patterns(self) -> List[str]:
        """Load exclude patterns from .vaultignore file or use defaults."""
        ignore_file = os.path.join(self.vault_path, ".vaultignore")
        patterns = [".obsidian", ".trash"] # Default exclusions
        
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
        """Check if a relative path matches any exclusion patterns."""
        path_parts = rel_path.split(os.sep)
        for pattern in self.exclude_patterns:
            # Match against each part of the path
            for part in path_parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            # Also match against the full relative path
            if fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def get_file_hash(self, file_path: str) -> str:
        """Calculate MD5 hash of a file."""
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            buf = f.read()
            hasher.update(buf)
        return hasher.hexdigest()

    def index_file(self, file_path: str):
        """Index a single markdown file."""
        if not file_path.endswith(".md"):
            return

        rel_path = os.path.relpath(file_path, self.vault_path)
        if self._is_excluded(rel_path):
            return

        logger.info(f"Indexing file: {rel_path}")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            file_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            
            # Check if file has changed
            existing = self.db.conn.execute(
                "SELECT md5 FROM documents WHERE path = ?", (rel_path,)
            ).fetchone()
            
            if existing and existing[0] == file_hash:
                logger.debug(f"File unchanged: {rel_path}")
                return

            # File changed or new, proceed to index
            metadata, body = self.parser.extract_metadata(content)
            chunks = self.parser.chunk_by_headers(body)
            
            # Use a transaction for the update
            self.db.conn.execute("BEGIN TRANSACTION")
            try:
                # 1. Update/Insert document (Manually handle cascade)
                self.db.conn.execute(
                    "DELETE FROM chunks WHERE document_path = ?", (rel_path,)
                )
                self.db.conn.execute(
                    "DELETE FROM documents WHERE path = ?", (rel_path,)
                )
                self.db.conn.execute(
                    "INSERT INTO documents (path, md5, metadata) VALUES (?, ?, ?)",
                    (rel_path, file_hash, json.dumps(metadata))
                )
                
                # 2. Generate embeddings for chunks and insert
                if chunks:
                    embeddings = self.model.encode(chunks)
                    for i, (chunk_text, vec) in enumerate(zip(chunks, embeddings)):
                        chunk_id = str(uuid.uuid4())
                        self.db.conn.execute(
                            "INSERT INTO chunks (chunk_id, document_path, content, embedding, metadata) VALUES (?, ?, ?, ?, ?)",
                            (chunk_id, rel_path, chunk_text, vec, json.dumps({"index": i}))
                        )
                
                self.db.conn.execute("COMMIT")
                logger.info(f"Successfully indexed {rel_path} ({len(chunks)} chunks)")
            except Exception as e:
                self.db.conn.execute("ROLLBACK")
                logger.error(f"Error during transaction for {rel_path}: {e}")
                
        except Exception as e:
            logger.error(f"Failed to index file {file_path}: {e}")

    def delete_file(self, file_path: str):
        """Remove a file from the index."""
        rel_path = os.path.relpath(file_path, self.vault_path)
        if self._is_excluded(rel_path):
            return

        logger.info(f"Deleting file from index: {rel_path}")
        self.db.conn.execute("DELETE FROM chunks WHERE document_path = ?", (rel_path,))
        self.db.conn.execute("DELETE FROM documents WHERE path = ?", (rel_path,))

    def full_sync(self):
        """Perform a full sync of the vault."""
        logger.info("Starting full sync...")
        
        # Get all files in DB to find deletions
        db_files = set(row[0] for row in self.db.conn.execute("SELECT path FROM documents").fetchall())
        current_files = set()

        for root, dirs, files in os.walk(self.vault_path):
            # filter dirs in-place to avoid traversing excluded directories
            dirs[:] = [d for d in dirs if not self._is_excluded(os.path.relpath(os.path.join(root, d), self.vault_path))]
            
            for file in files:
                if file.endswith(".md"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.vault_path)
                    if not self._is_excluded(rel_path):
                        current_files.add(rel_path)
                        self.index_file(full_path)
        
        # Remove files that no longer exist
        deleted_files = db_files - current_files
        for rel_path in deleted_files:
            logger.info(f"Removing deleted file: {rel_path}")
            self.db.conn.execute("DELETE FROM chunks WHERE document_path = ?", (rel_path,))
            self.db.conn.execute("DELETE FROM documents WHERE path = ?", (rel_path,))
            
        logger.info("Full sync complete")

class VaultWatchdogHandler(FileSystemEventHandler):
    def __init__(self, indexer: VaultIndexer):
        self.indexer = indexer

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.index_file(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.index_file(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self.indexer.delete_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if event.src_path.endswith(".md"):
                self.indexer.delete_file(event.src_path)
            if event.dest_path.endswith(".md"):
                self.indexer.index_file(event.dest_path)

def start_watcher(vault_path: str, indexer: VaultIndexer):
    """Start the watchdog observer to monitor the vault."""
    event_handler = VaultWatchdogHandler(indexer)
    observer = Observer()
    observer.schedule(event_handler, vault_path, recursive=True)
    observer.start()
    logger.info(f"Started monitoring vault at: {vault_path}")
    return observer
