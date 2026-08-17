"""Hermetic database behavior tests."""


def test_schema_and_linear_vector_search_work_without_vss(database_factory):
    db = database_factory()
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
    assert hnsw_index is None
