import pytest

from e5embed.daemon import EmbeddingDaemon, PRIORITIES


class FakeVector(list):
    def tolist(self):
        return list(self)


class FakeModel:
    last_texts = None
    last_normalize = None

    def __init__(self, model_path):
        self.model_path = model_path

    def encode(self, texts, normalize_embeddings=True):
        FakeModel.last_texts = texts
        FakeModel.last_normalize = normalize_embeddings
        base = 1.0 if normalize_embeddings else 2.0
        return [FakeVector([base + i, base + i + 0.5]) for i, _ in enumerate(texts)]


def test_daemon_embeds_batch_with_query_prefix(tmp_path):
    daemon = EmbeddingDaemon(tmp_path, model_class=FakeModel)
    try:
        result = daemon.embed(["検索語", "別の検索語"], embed_type="query", priority="high")
    finally:
        daemon.shutdown()

    assert FakeModel.last_texts == ["query: 検索語", "query: 別の検索語"]
    assert FakeModel.last_normalize is True
    assert result["dimension"] == 2
    assert result["count"] == 2
    assert result["embeddings"] == [[1.0, 1.5], [2.0, 2.5]]


def test_priorities_put_high_before_low():
    assert PRIORITIES["high"] < PRIORITIES["normal"] < PRIORITIES["low"]


def test_daemon_rejects_empty_text_without_dropping_index(tmp_path):
    daemon = EmbeddingDaemon(tmp_path, model_class=FakeModel)
    try:
        with pytest.raises(ValueError) as exc_info:
            daemon.embed(["valid", "  "], embed_type="passage", priority="low")
        assert "texts[1]" in str(exc_info.value)
    finally:
        daemon.shutdown()
