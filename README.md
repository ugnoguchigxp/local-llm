# Local LLM Runtime (Gemma 4 + Qwen 2.5 + Bonsai + Embedding)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey.svg)](https://developer.apple.com/metal/tensorflow-plugin/)

このプロジェクトは、Apple Silicon (MLX) や Ollama を活用して、高性能な LLM ローカル実行環境、埋め込み (Embedding) サーバー、および自律型エージェント機能を提供します。Zed や VSCode (Continue) などの IDE から利用可能な OpenAI 互換 API を内蔵しており、完全ローカルでセキュアな RAG (Retrieval-Augmented Generation) 環境を構築可能です。

---

## ✨ 主な特徴

- **マルチバックエンド対応**: `MLX` (Apple Silicon 最適化), `Ollama`, `Bonsai` をシームレスに切り替え。
- **最新モデルのフルサポート**:
  - **Gemma 4 (E4B)**: MTP 推論による高速生成。
  - **Qwen 2.5**: 推論性能に優れた Qwen シリーズを MLX バックエンドで実行。
  - **Bonsai**: 極小量子化 (2-bit) でも高い性能を発揮する 8B モデル。
- **ローカル Embedding サーバー**: `multilingual-e5-small` を使用した高性能な埋め込み API。
- **自律型エージェント (MCP)**: Web検索 (`Brave Search`) やウェブスクレイピング機能を標準搭載。
- **OpenAI 互換 API**: Chat Completions (`/v1/chat/completions`) および Embeddings (`/v1/embeddings`) を提供。
- **セッション管理**: 会話履歴を自動保存し、CLI からの継続的な対話が可能。

---

## 💻 ハードウェア要件

- **推奨**: Apple Silicon (M1/M2/M3/M4) 搭載の Mac
  - MLX バックエンドの性能を最大限に引き出すために必要です。
  - メモリ (Unified Memory): 16GB 以上推奨（8GB でも動作しますが、量子化モデルを推奨）。
- **その他**: Intel Mac や Linux/Windows
  - Ollama バックエンド経由での利用が可能です。

---

## 🚀 セットアップ手順

### 1. リポジトリの準備

```bash
git clone https://github.com/YOUR_USERNAME/localLlm.git
cd localLlm
```

### 2. 仮想環境と依存関係のインストール

```bash
# 全体セットアップスクリプトの実行（推奨）
bash scripts/setup.sh
```

このスクリプトは、ルートディレクトリと `embedding/` ディレクトリの両方の仮想環境をセットアップします。

### 3. 環境変数の設定

```bash
cp .env.example .env
```

`.env` 内の `BRAVE_SEARCH_API_KEY` を設定すると、Web検索ツールが有効になります。

---

## 🛠 使い方と得られる結果

### 1. CLI での対話 (各モデル)

使用したいモデルに合わせてスクリプトを使い分けます。初回実行時にモデルが自動ダウンロードされます。

```bash
# Gemma 4 を使用
./scripts/gemma4 "こんにちは"

# Qwen 2.5 を使用
./scripts/qwen "複雑な数学の問題を解いて"

# Bonsai (8B) を使用
./scripts/bonsai "自己紹介してください"
```

### 2. OpenAI 互換 API サーバー (Chat)

IDE (Zed, VSCode 等) から利用する場合に起動します。

```bash
./scripts/run_openai_api.sh
```
- **Port**: `44448`
- **Endpoint**: `http://localhost:44448/v1/chat/completions`

### 3. Embedding サーバーの起動

RAG や文書検索などのために埋め込みベクトルを生成する場合に起動します。

```bash
./scripts/run_embedding_daemon.sh
```
- **Port**: `44512`
- **Model**: `multilingual-e5-small` (デフォルト)
- **Endpoint**: `http://localhost:44512/v1/embeddings`

### 4. 自律型エージェント (MCP Tools)

Brave Search 等のツールを使用するエージェント機能を起動します。

```bash
python mcp/tools_server.py
```

---

## ⚙️ 環境変数の詳細 (.env)

| 変数名 | 説明 | 既定値 |
| :--- | :--- | :--- |
| `GEMMA4_MODEL` | MLX で使用する Gemma 4 モデル | `mlx-community/gemma-4-e4b-it-4bit` |
| `QWEN_MODEL` | MLX で使用する Qwen モデル | `mlx-community/Qwen2.5-14B-Instruct-4bit` |
| `GEMMA4_API_PORT` | Chat API サーバーのポート | `44448` |
| `EMBEDDING_API_PORT` | Embedding API のポート | `44512` |
| `BRAVE_SEARCH_API_KEY` | Brave Search API キー | (空) |

---

## 🔍 トラブルシューティング

### 環境診断
現在のセットアップが正しいか確認するには：
```bash
./scripts/doctor
```

### ステータス確認
```bash
./scripts/status
```

---

## 📂 ディレクトリ構成

- `scripts/`: 各種モデル実行 (gemma4, qwen, bonsai) および API 起動スクリプト。
- `embedding/`: E5 埋め込みサーバー専用のソースコードと仮想環境。
- `api/`: FastAPI による OpenAI 互換 Chat API。
- `core/`: 推論・セッション管理のコアロジック。
- `backends/`: MLX, Ollama 等のバックエンド抽象化層。
- `mcp/`: Model Context Protocol サーバー。

---

## 📜 ライセンス

[MIT License](LICENSE)
