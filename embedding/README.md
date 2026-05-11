# multilingual-e5-small local setup

このフォルダで `intfloat/multilingual-e5-small` をローカル利用する最小構成です。

## 1. CLI インストール（PATH で直接実行）

```bash
bash scripts/install_cli.sh
```

上記で以下が実行されます:

- `.venv` 作成（未作成時）
- 依存インストール
- editable install（`e5embed` / `embed` コマンド生成）
- `~/.local/bin/e5embed` にシンボリックリンク作成（`PATH` から直接実行可）
- `~/.local/bin/embed` にシンボリックリンク作成（`PATH` から直接実行可）

## 2. モデルをローカル保存

```bash
source .venv/bin/activate
python scripts/download_model.py
```

保存先:

- `models/multilingual-e5-small`

## 3. CLI 動作確認

```bash
e5embed --type query --text "日本の首都は？"
e5embed --type passage --text "東京は日本の首都です。" --text "富士山は日本一高い山です。" --pretty
embed "対象のテキスト"
```

`query:` と `passage:` の接頭辞を付けて埋め込みするのが E5 系モデルの推奨です。
`embed` は `passage:` として 1件だけ埋め込み、ベクトル配列(JSON)を返します。

必要ならモデルディレクトリを明示できます:

```bash
e5embed --model-dir /path/to/multilingual-e5-small --type query --text "日本の首都は？"
```

JSON でベクトルを出力します（`dimension` は 384）。

## 4. テスト実行

```bash
source .venv/bin/activate
pytest -q
```
