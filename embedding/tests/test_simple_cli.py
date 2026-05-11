import json

import pytest

from e5embed import simple_cli


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
        return [FakeVector([0.1, 0.2, 0.3])]


def test_main_embeds_single_text(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(simple_cli.core, "get_model_class", lambda: FakeModel)

    code = simple_cli.main([
        "--model-dir",
        str(tmp_path),
        "対象のテキスト",
    ])

    assert code == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == [0.1, 0.2, 0.3]
    assert FakeModel.last_texts == ["passage: 対象のテキスト"]
    assert FakeModel.last_normalize is True


def test_main_raises_if_model_missing():
    with pytest.raises(FileNotFoundError):
        simple_cli.main(["--model-dir", "/tmp/not-found-model-dir-for-embed", "abc"])
