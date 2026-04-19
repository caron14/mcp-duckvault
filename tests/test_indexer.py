import os
import pytest
from mcp_duckvault.indexer import MarkdownParser, VaultIndexer
from mcp_duckvault.db_manager import DatabaseManager

def test_markdown_parser_frontmatter():
    content = "---\ntitle: Test Note\ntags: [tag1, tag2]\n---\n# Header\nContent"
    metadata, body = MarkdownParser.extract_metadata(content)
    assert metadata == {"title": "Test Note", "tags": ["tag1", "tag2"]}
    assert body.strip() == "# Header\nContent"

def test_markdown_parser_no_frontmatter():
    content = "# Header\nContent"
    metadata, body = MarkdownParser.extract_metadata(content)
    assert metadata == {}
    assert body.strip() == "# Header\nContent"

def test_markdown_chunk_by_headers():
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
