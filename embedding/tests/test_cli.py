import json
from types import SimpleNamespace

import pytest

from e5embed import cli


class FakeVector(list):
    def tolist(self):
        return list(self)


class FakeModel:
    def __init__(self, model_path):
        self.model_path = model_path

    def encode(self, texts, normalize_embeddings=True):
        base = 1.0 if normalize_embeddings else 2.0
        return [FakeVector([base + i, base + i + 0.5]) for i, _ in enumerate(texts)]


def test_run_adds_e5_prefix_and_dimension(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_model_class", lambda: FakeModel)
    args = SimpleNamespace(
        model_dir=str(tmp_path),
        type="query",
        text=["日本の首都は？", "富士山の高さは？"],
        no_normalize=False,
        pretty=False,
    )

    rows = cli.run(args)

    assert rows[0]["prefixed"] == "query: 日本の首都は？"
    assert rows[1]["prefixed"] == "query: 富士山の高さは？"
    assert rows[0]["dimension"] == 2
    assert rows[1]["dimension"] == 2


def test_run_raises_if_model_missing():
    args = SimpleNamespace(
        model_dir="/tmp/not-found-model-dir-for-e5",
        type="passage",
        text=["abc"],
        no_normalize=False,
        pretty=False,
    )

    with pytest.raises(FileNotFoundError):
        cli.run(args)


def test_main_prints_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_model_class", lambda: FakeModel)

    exit_code = cli.main([
        "--model-dir",
        str(tmp_path),
        "--type",
        "passage",
        "--text",
        "東京は日本の首都です。",
        "--pretty",
    ])

    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload[0]["prefixed"] == "passage: 東京は日本の首都です。"
    assert payload[0]["dimension"] == 2
