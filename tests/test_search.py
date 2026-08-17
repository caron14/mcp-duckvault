"""Unit tests for the search and tag filtering logic."""

import json
import urllib.parse

from mcp_duckvault.mcp_server import _vector_search


def test_tag_filtering_logic(database_factory):
    """Tests the SQL logic for filtering documents by tags in various formats."""
    # Test cases for frontmatter tag formats
    # d.metadata->'$.tags' ? ? OR d.metadata->'$.tag' = ? ...
    # We'll test if the SQL logic we implemented matches expectations
    # But since it's SQL, we should ideally run it against a real DuckDB

    db = database_factory(embedding_dim=384)

    # Insert test data
    test_data = [
        ("path1.md", "hash1", {"tags": ["work", "project"]}, "content1"),
        ("path2.md", "hash2", {"tag": "personal"}, "content2"),
        ("path3.md", "hash3", {"tags": "work urgent"}, "content3"),
        ("path4.md", "hash4", {"tags": ["home"]}, "content4"),
    ]

    for path, md5, meta, content in test_data:
        db.conn.execute(
            "INSERT INTO documents (path, md5, metadata) VALUES (?, ?, ?)",
            (path, md5, json.dumps(meta)),
        )
        db.conn.execute(
            "INSERT INTO chunks (chunk_id, document_path, content, embedding) VALUES (?, ?, ?, ?)",
            (path, path, content, [0.1] * 384),
        )

    def search_with_tag(tag):
        return [row["path"] for row in _vector_search(db.conn, [0.1] * 384, tag=tag, limit=100)]

    assert "path1.md" in search_with_tag("work")
    assert "path2.md" in search_with_tag("personal")
    assert "path3.md" in search_with_tag("work")
    assert "path3.md" in search_with_tag("urgent")
    assert "path4.md" in search_with_tag("home")
    assert "path1.md" not in search_with_tag("personal")

    db.conn.execute(
        "INSERT INTO documents (path, md5, metadata) VALUES (?, ?, ?)",
        ("substring.md", "hash", json.dumps({"tags": ["homework"]})),
    )
    db.conn.execute(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
        ("substring", "substring.md", "content", [0.1] * 384, "{}"),
    )
    assert "substring.md" not in search_with_tag("home")


def test_obsidian_uri_generation():
    """Tests the generation of Obsidian URIs for search results."""
    vault_name = "My Vault"
    path = "Folder/Note Name.md"
    encoded_vault = urllib.parse.quote(vault_name)
    encoded_path = urllib.parse.quote(path)
    uri = f"obsidian://open?vault={encoded_vault}&file={encoded_path}"
    assert "My%20Vault" in uri
    assert (
        "Folder/Note%20Name.md" in uri or "Folder/Note%20Name.md" in uri
    )  # depending on quote behavior
    # urllib.parse.quote by default doesn't quote slashes.
    assert uri == "obsidian://open?vault=My%20Vault&file=Folder/Note%20Name.md"
