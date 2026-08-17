"""Unit tests for the VaultIndexer and MarkdownParser classes."""

import os

import pytest

from mcp_duckvault.db_manager import DatabaseManager
from mcp_duckvault.indexer import MarkdownParser, VaultIndexer


class FakeEmbeddingModel:
    def encode(self, texts, is_query=False):
        del is_query
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_markdown_parser_frontmatter():
    """Tests extraction of YAML frontmatter from Markdown content."""
    content = "---\ntitle: Test Note\ntags: [tag1, tag2]\n---\n# Header\nContent"
    metadata, body = MarkdownParser.extract_metadata(content)
    assert metadata == {"title": "Test Note", "tags": ["tag1", "tag2"]}
    assert body.strip() == "# Header\nContent"


def test_markdown_parser_no_frontmatter():
    """Tests Markdown parsing when no frontmatter is present."""
    content = "# Header\nContent"
    metadata, body = MarkdownParser.extract_metadata(content)
    assert metadata == {}
    assert body.strip() == "# Header\nContent"


def test_markdown_chunk_by_headers():
    """Tests chunking of Markdown content by H1-H3 headers."""
    content = """# H1
Content 1
## H2
Content 2
### H3
Content 3
#### H4 (not a split)
Content 4"""
    chunks = MarkdownParser.chunk_by_headers(content)
    assert len(chunks) == 3
    assert chunks[0].startswith("# H1")
    assert chunks[1].startswith("## H2")
    assert chunks[2].startswith("### H3")
    assert "#### H4" in chunks[2]


def test_vault_indexer_exclusion(tmp_path):
    """Tests default exclusion patterns in the VaultIndexer."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / ".obsidian").mkdir()
    (vault_path / "note.md").write_text("content")
    (vault_path / ".obsidian" / "config").write_text("config")
    (vault_path / ".trash").mkdir()
    (vault_path / ".trash" / "deleted.md").write_text("deleted")

    db_manager = DatabaseManager(":memory:")
    indexer = VaultIndexer(str(vault_path), db_manager)

    assert indexer._is_excluded(".obsidian/config")
    assert indexer._is_excluded(".trash/deleted.md")
    assert not indexer._is_excluded("note.md")


def test_vault_ignore_exclusion(tmp_path):
    """Tests custom exclusion patterns from .vaultignore."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / ".vaultignore").write_text("private/\n*.log")
    (vault_path / "private").mkdir()
    (vault_path / "private" / "secret.md").write_text("secret")
    (vault_path / "app.log").write_text("log")
    (vault_path / "note.md").write_text("content")

    db_manager = DatabaseManager(":memory:")
    indexer = VaultIndexer(str(vault_path), db_manager)

    assert indexer._is_excluded("private/secret.md")
    assert indexer._is_excluded("app.log")
    assert not indexer._is_excluded("note.md")


def test_sync_plan_is_read_only_and_reports_paths(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    unchanged = vault / "unchanged.md"
    unchanged.write_text("same", encoding="utf-8")
    changed = vault / "changed.md"
    changed.write_text("new", encoding="utf-8")
    (vault / ".trash").mkdir()
    (vault / ".trash" / "ignored.md").write_text("ignored", encoding="utf-8")
    db = database_factory()
    indexer = VaultIndexer(str(vault), db, model=FakeEmbeddingModel())
    indexer.index_file(str(unchanged))
    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata, index_signature) VALUES (?, ?, '{}', ?)",
        ("deleted.md", "old", db._config("index_signature")),
    )
    before = db.conn.execute("SELECT path, md5 FROM documents ORDER BY path").fetchall()

    plan = indexer.plan_sync()

    assert plan.indexed == 1
    assert plan.skipped == 1
    assert plan.deleted == 1
    assert plan.excluded == 1
    assert plan.paths["indexed"] == ["changed.md"]
    assert plan.paths["skipped"] == ["unchanged.md"]
    assert plan.paths["deleted"] == ["deleted.md"]
    assert db.conn.execute("SELECT path, md5 FROM documents ORDER BY path").fetchall() == before


def test_large_file_is_reported_without_replacing_old_index(tmp_path, database_factory):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("old", encoding="utf-8")
    db = database_factory()
    initial = VaultIndexer(str(vault), db, model=FakeEmbeddingModel(), max_file_size=100)
    assert initial.full_sync().status == "complete"
    note.write_text("x" * 101, encoding="utf-8")

    indexer = VaultIndexer(str(vault), db, model=FakeEmbeddingModel(), max_file_size=100)
    plan = indexer.plan_sync()
    summary = indexer.full_sync()

    assert plan.failed == 1
    assert plan.reasons["note.md"].startswith("file_too_large:")
    assert summary.status == "failed"
    assert summary.failures[0].error_code == "FILE_TOO_LARGE"
    assert db.conn.execute("SELECT content FROM chunks").fetchone() == ("old",)


def test_symlink_files_and_directories_are_never_scanned(tmp_path, database_factory):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("outside", encoding="utf-8")
    try:
        (vault / "linked.md").symlink_to(outside / "secret.md")
        (vault / "linked-dir").symlink_to(outside, target_is_directory=True)
        (vault / "loop").symlink_to(vault, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    indexer = VaultIndexer(str(vault), database_factory(), model=FakeEmbeddingModel())

    plan = indexer.plan_sync()

    assert plan.scanned == 0
    assert plan.excluded == 3
    assert set(plan.reasons.values()) == {"symlink_not_allowed"}
