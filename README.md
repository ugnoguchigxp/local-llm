# Gemma 4 Local Runtime (MLX + Ollama + Bonsai)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey.svg)](https://developer.apple.com/metal/tensorflow-plugin/)

このプロジェクトは、Apple Silicon (MLX) や Ollama を活用して、高性能な LLM ローカル実行環境および自律型エージェント機能を提供します。Zed や VSCode (Continue) などの IDE から利用可能な OpenAI 互換 API サーバーを内蔵しており、ローカル完結でセキュアかつ高速な開発体験を実現します。

---

## ✨ 主な特徴

- **マルチバックエンド対応**: Apple Silicon に最適化された `MLX` と、汎用的な `Ollama` をシームレスに切り替え。
- **高性能モデルのサポート**: Gemma 4 (E4B), Bonsai, Qwen 2.5 等の最新モデルに対応。
- **自律型エージェント (MCP)**: Web検索 (`Brave Search`) やウェブスクレイピング機能を標準搭載。
- **OpenAI 互換 API**: `/v1/chat/completions` エンドポイントを提供し、既存ツールから即座に利用可能。
- **MTP 推論 (Speculative Decoding)**: Gemma 4 等での高速なトークン生成。
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
# セットアップスクリプトの実行（推奨）
bash scripts/setup.sh
```

または手動で：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 環境変数の設定

```bash
cp .env.example .env
```

`.env` ファイルを開き、必要に応じて設定を調整してください：
- `BRAVE_SEARCH_API_KEY`: Web検索機能を有効にする場合に設定。
- `GEMMA4_MODEL`: 使用する MLX モデルのパスまたは HuggingFace ID。

---

## 🛠 使い方と得られる結果

### 1. CLI での対話 (Gemma 4)

最も手軽にモデルと対話する方法です。初回実行時にモデルが自動ダウンロードされます。

```bash
./scripts/gemma4 "Rustの所有権について教えて"
```

**得られる結果 (JSON出力):**
デフォルトでは、他のプログラムから扱いやすいようにセッションIDを含む JSON が返ります。
```json
{
  "session_id": "sess_12345",
  "response": "Rustの所有権（Ownership）は、メモリ安全性を保証するための主要な概念です...",
  "model": "mlx-community/gemma-4-e4b-it-4bit",
  "usage": { "prompt_tokens": 15, "completion_tokens": 120 }
}
```

テキストのみが必要な場合：
```bash
./scripts/gemma4 --prompt "挨拶して" --output text
```

### 2. OpenAI 互換 API サーバーの起動

Zed や VSCode などの IDE から利用する場合に起動します。

```bash
./scripts/run_openai_api.sh
```

起動後、`http://localhost:44448/v1/chat/completions` でリクエストを受け付けます。

### 3. 自律型エージェント (MCP Tools)

Brave Search 等のツールを使用するエージェント機能を起動します。

```bash
python mcp/tools_server.py
```

Claude Desktop や Gnosis などの MCP クライアントに設定することで、LLM が必要に応じてウェブ検索を行い、最新の情報に基づいた回答を生成します。

---

## ⚙️ 環境変数の詳細 (.env)

| 変数名 | 説明 | 既定値 |
| :--- | :--- | :--- |
| `GEMMA4_MODEL` | MLX で使用する Gemma 4 モデル | `mlx-community/gemma-4-e4b-it-4bit` |
| `GEMMA4_API_PORT` | API サーバーのポート番号 | `44448` |
| `GEMMA4_MTP_ENABLED` | MTP (高速推論) を有効にするか | `false` |
| `BRAVE_SEARCH_API_KEY` | Brave Search API キー | (空) |
| `LOCAL_LLM_CONTEXT_WINDOW` | コンテキストウィンドウサイズ | `131072` |

---

## 🔍 トラブルシューティング

### MLX が Metal 初期化でエラーになる
Sandbox 環境や一部のターミナル環境で Metal の初期化に失敗する場合があります。
```bash
# 安全停止をバイパスして強制実行する場合
LOCAL_LLM_ALLOW_MLX_IN_SEATBELT=1 ./scripts/gemma4 "hello"
```

### セットアップの確認
現在の環境が正しく設定されているか確認するには：
```bash
./scripts/doctor
```

### 動作ステータスの確認
```bash
./scripts/status
```

---

## 📂 ディレクトリ構成

- `scripts/`: 各種モデル実行用ショートカット、管理用スクリプト。
- `api/`: FastAPI による OpenAI 互換 API の実装。
- `core/`: 推論ロジック、セッション管理のコア。
- `backends/`: MLX, Ollama, Llama.cpp 等の抽象化層。
- `mcp/`: Model Context Protocol サーバーの実装。
- `tools.py`: Web検索、スクレイピング等のツール実装。

---

## 📜 ライセンス

[MIT License](LICENSE)
