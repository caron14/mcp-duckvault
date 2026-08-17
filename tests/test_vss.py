"""DuckDB VSS integration smoke test."""

import pytest

from mcp_duckvault.db_manager import DatabaseManager


@pytest.mark.vss
@pytest.mark.timeout(180)
def test_vss_install_load_hnsw_and_search(tmp_path):
    db_path = tmp_path / "vss-smoke.db"
    DatabaseManager.prepare_vss(str(db_path))

    db = DatabaseManager(str(db_path))
    try:
        db.connect()
        db.initialize_schema(embedding_dim=4)
        db.conn.execute(
            "INSERT INTO chunks (chunk_id, document_path, content, embedding) "
            "VALUES ('chunk', 'note.md', 'body', ?)",
            [[1.0, 0.0, 0.0, 0.0]],
        )

        score = db.conn.execute(
            "SELECT 1 - (embedding <=> ?::FLOAT[]) FROM chunks",
            [[1.0, 0.0, 0.0, 0.0]],
        ).fetchone()[0]
        hnsw_index = db.conn.execute(
            "SELECT 1 FROM duckdb_indexes() WHERE index_name = 'chunk_vec_idx'"
        ).fetchone()

        assert score == 1.0
        assert hnsw_index == (1,)
    finally:
        db.close()

    read_only = DatabaseManager(str(db_path), read_only=True)
    try:
        read_only.connect(load_vss=False)
        assert read_only.conn.execute("SELECT count(*) FROM chunks").fetchone() == (1,)
    finally:
        read_only.close()
