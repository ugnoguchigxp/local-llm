# local-llm (API Server First)

`local-llm` は **OpenAI 互換 API サーバー** としてローカルモデルを提供するためのランタイムです。  
コーディングエージェントの UI・コード編集・CLI 実行はこのリポジトリの責務ではなく、**呼び出し元クライアント**（Zed など）側で実装する前提です。

## 目的

- API サーバー責務に集中
  - モデルロード / 推論
  - OpenAI 互換エンドポイント
  - 認証・ヘルスチェック
- クライアント責務を分離
  - UI
  - ツール実行
  - エージェントループ

## 提供エンドポイント

- `GET /health`
- `GET /status`
- `GET /v1/models`
- `POST /v1/chat/completions`

`/v1/chat/completions` は `tool_calls` を返せます。  
ただし **ツール実行はサーバーでは行いません**。ツール実行は呼び出し元クライアントで行ってください。

## セットアップ

```bash
git clone https://github.com/YOUR_USERNAME/localLlm.git
cd localLlm
bash scripts/setup.sh
cp .env.example .env
```

## API サーバー起動

```bash
./scripts/run_openai_api.sh
```

デフォルト: `http://127.0.0.1:44448`

`run_openai_api.sh` は既定で `LOCAL_LLM_GPU_SAFE_MODE=true` として起動し、Metal GPU timeout を避けるために以下を保守設定へ寄せます（未設定時のみ）。

- `LOCAL_LLM_PREFILL_STEP_SIZE=2048`
- `LOCAL_LLM_CONTEXT_WINDOW=81920`
- `LOCAL_LLM_MAX_PROMPT_TOKENS=60000`
- `LOCAL_LLM_MAX_OUTPUT_TOKENS=20000`
- `LOCAL_LLM_MAX_TOOL_CALL_TOKENS=20000`

速度優先で戻したい場合のみ `LOCAL_LLM_GPU_SAFE_MODE=false` を設定してください。

## API 動作確認

```bash
./scripts/status
curl http://127.0.0.1:44448/v1/models
```

## launchd 自動スタート制御

LLM API / embedding daemon の自動スタートを一時的に止める場合:

```bash
pnpm stop
```

再開する場合:

```bash
pnpm start
```

状態確認:

```bash
pnpm status
```

## MTP ベンチマークと自動切替

同一条件で `MTP OFF/ON` を比較し、`ON` が既定以上速ければ `.env` の `GEMMA4_MTP_ENABLED=true` を自動設定します。

```bash
./.venv/bin/python scripts/benchmark_and_toggle_mtp.py --repeats 2 --warmup 1 --max-tokens 256 --min-speedup 1.10
```

`speedup >= min_speedup` の場合のみ `GEMMA4_MTP_ENABLED=true`、それ以外は `false` になります。

## Zed から利用

Zed の `OpenAI Compatible` プロバイダで次を設定します。

- API URL: `http://127.0.0.1:44448/v1`
- Model: `gemma-4-e4b-it` / `qwen-3.6-14b-it` / `bonsai-8b-2bit`

参考: [Zed LLM Providers](https://zed.dev/docs/ai/llm-providers)

## CLI（外部 API 呼び出しクライアント）

`main.py` は API クライアントとして動作します。

```bash
# 単発
./scripts/gemma4 "FastAPIでJWT検証ミドルウェアの例を書いて"

# API URL を指定
python3 main.py --api-base http://127.0.0.1:44448 --model qwen-3.6-14b-it "Rustの所有権を説明して"

# 対話
python3 main.py --model gemma-4-e4b-it
```

### CLIのローカルツール

CLI は最小ツールとして以下をサポートします。

- `search_web(query)`
- `fetch_content(url)`

このツールは **CLI 側** で実行されます。  
サーバー側は tool call の返却のみを担当します。

## モデル設定

`.env` 例:

- `GEMMA4_MODEL` / `GEMMA4_API_MODEL_ID`
- `QWEN_MODEL` / `QWEN_API_MODEL_ID`
- `BONSAI_MODEL` / `BONSAI_API_MODEL_ID`
- `LOCAL_LLM_GPU_SAFE_MODE`（既定 `true`）
- `LOCAL_LLM_MAX_PROMPT_TOKENS`
- `LOCAL_LLM_MAX_OUTPUT_TOKENS`
- `LOCAL_LLM_MAX_TOOL_CALL_TOKENS`
- `LOCAL_LLM_PREFILL_STEP_SIZE`
- `LOCAL_LLM_CONTEXT_WINDOW`

## 認証

- `LOCAL_LLM_REQUIRE_AUTH=true` で Bearer 認証を有効化
- `LOCAL_LLM_ACCESS_TOKEN=<token>` を設定

## ライセンス

MIT
