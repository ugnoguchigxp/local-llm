# Local LLM Runtime (Gemma 4 + Qwen 3.6 + Bonsai + Embedding)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey.svg)](https://developer.apple.com/metal/tensorflow-plugin/)

このプロジェクトは、Apple Silicon (MLX) や Ollama を活用して、高性能な LLM ローカル実行環境、埋め込み (Embedding) サーバー、および自律型エージェント機能を提供します。M4 Mac 等の最新ハードウェアに最適化されたモデル構成を採用しており、完全ローカルでセキュアな RAG 環境を構築可能です。

---

## ✨ 主な特徴

- **マルチバックエンド対応**: `MLX` (Apple Silicon 最適化), `Ollama`, `Bonsai` をシームレスに切り替え。
- **最新・最適化モデルのサポート (May 2026)**:
  - **Gemma 4 (E4B)**: Google の最新軽量モデル。MTP 推論による高速生成に対応。
  - **Qwen 3.6 (14B)**: コーディングやエージェントタスクに最適な最新 Qwen。標準的な M4 Mac (16GB RAM) でも軽快に動作する 14B モデルを採用。
  - **Bonsai**: 2-bit 量子化でも高い知能を維持する極限効率モデル。
- **ローカル Embedding サーバー**: `multilingual-e5-small` による高性能な埋め込み API。
- **自律型エージェント (MCP)**: Web検索 (`Brave Search`) やウェブスクレイピング機能を標準搭載。
- **OpenAI 互換 API**: Chat Completions および Embeddings を提供。

---

## 💻 ハードウェア要件

- **推奨**: Apple Silicon (M1/M2/M3/M4) 搭載の Mac
  - **M4 Mac 推奨**: Qwen 3.6 MoE モデルなどの最新アーキテクチャを快適に動作させるために最適です。
  - メモリ (Unified Memory): 16GB 以上推奨（32GB 以上で Qwen 3.6 35B がより快適に動作します）。

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

### 3. 環境変数の設定

```bash
cp .env.example .env
```

---

## 🛠 使い方と得られる結果

### 1. CLI での対話

最新の量子化済み MLX モデルを即座に試せます。

```bash
# Gemma 4 を使用 (高速)
./scripts/gemma4 "こんにちは"

# Qwen 3.6 を使用 (高度なコーディング・推論)
./scripts/qwen "複雑な Rust コードを書いて"

# Bonsai を使用 (低メモリ消費)
./scripts/bonsai "自己紹介してください"
```

### 2. API サーバーの起動

```bash
# Chat API (Port: 44448)
./scripts/run_openai_api.sh

# Embedding API (Port: 44512)
./scripts/run_embedding_daemon.sh
```

---

## ⚙️ 環境変数の詳細 (.env)

| 変数名 | 説明 | 既定値 |
| :--- | :--- | :--- |
| `GEMMA4_MODEL` | MLX で使用する Gemma モデル | `mlx-community/gemma-4-e4b-it-4bit` |
| `QWEN_MODEL` | MLX で使用する Qwen モデル | `mlx-community/Qwen3.6-14B-4bit` |
| `GEMMA4_API_PORT` | Chat API サーバーのポート | `44448` |
| `EMBEDDING_API_PORT` | Embedding API のポート | `44512` |

---

## 🔍 トラブルシューティング

### 環境診断
```bash
./scripts/doctor
```

### ステータス確認
```bash
./scripts/status
```

---

## 📜 ライセンス

[MIT License](LICENSE)
