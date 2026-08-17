"""Normal runtime must not silently fall back to a model download."""

import pytest

from mcp_duckvault import indexer


def test_embedding_model_can_disable_download(monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise OSError("not cached")

    monkeypatch.setattr(indexer, "SentenceTransformer", unavailable)
    monkeypatch.setattr(indexer, "snapshot_download", lambda **_kwargs: "/already-cached-model")
    model = indexer.EmbeddingModel(allow_download=False)

    with pytest.raises(OSError):
        model.encode(["query"])
    assert calls == [{"local_files_only": True}]
