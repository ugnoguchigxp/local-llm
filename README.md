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
- `POST /v1/responses`

`/v1/chat/completions` は `tool_calls` を返せます。  
ただし **ツール実行はサーバーでは行いません**。ツール実行は呼び出し元クライアントで行ってください。

### コーディングエージェント provider 契約

- `tool_choice: "none"` では tools をモデルへ渡さず、tool call parsing も行いません。
- `tool_choice: "required"` で tool call が生成されなかった場合は `required_tool_call_missing` を返します。
- 復元した tool call の `arguments` は、`tools[].function.parameters` の基本 schema（`object` / `required` / `properties` / 基本型）で検証します。
- schema 不適合の tool call は、同じ schema とエラー理由を添えて 1 回だけ JSON-only retry します。retry 後も不正なら `invalid_tool_arguments` を返します。
- tools ありの streaming は、生成を一度 buffer してから `delta.tool_calls` と final `finish_reason: "tool_calls"` を返します。tools なしの streaming は daemon から live chunk を返します。
- parallel tool calls は未対応です。対応するまで、クライアント側では 1 turn 1 tool call 前提で扱ってください。
- `top_p` と `stop` は API から model manager まで渡します。model profile の stop sequence と重複しない形で後処理します。
- max token 到達が generation stats から判断できる場合は `finish_reason: "length"` を返します。

### Provider profile

`core/provider_profiles.py` でモデルファミリーごとの provider profile を定義します。

- `gemma`: recommended-after-smoke
- `qwen`: recommended-after-smoke
- `bonsai`: experimental

profile は thinking 抑制 instruction、special token sanitize、stop sequence、推奨 `temperature` / `top_p` を持ちます。新しいモデルを追加する場合は、通常チャット性能ではなく次の基準で確認してください。

- tool call JSON が安定している
- tool result 後の follow-up ができる
- special token / thinking が表に出ない
- long context で system/tool 定義を落とさない
- 量子化後も JSON と patch 生成が崩れにくい

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
- `LOCAL_LLM_CONTEXT_WINDOW=176000`
- `LOCAL_LLM_MAX_PROMPT_TOKENS=160000`
- `LOCAL_LLM_MAX_OUTPUT_TOKENS=20000`
- `LOCAL_LLM_MAX_TOOL_CALL_TOKENS=20000`

速度優先で戻したい場合のみ `LOCAL_LLM_GPU_SAFE_MODE=false` を設定してください。

## API 動作確認

```bash
./scripts/status
curl http://127.0.0.1:44448/v1/models
```

provider contract の live smoke:

```bash
./.venv/bin/python scripts/smoke_provider_contract.py --base-url http://127.0.0.1:44448/v1 --model gemma-4-12b-it-4bit
```

軽い疎通だけ確認する場合:

```bash
./.venv/bin/python scripts/smoke_provider_contract.py --base-url http://127.0.0.1:44448/v1 --model gemma-4-12b-it-4bit --checks models,invalid-tool-choice
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

`LOCAL_LLM_CONTEXT_WINDOW` はホストが試験的に許可するコンテキスト上限です。リクエストごとの安全なプロンプト予算は、daemon が `contextWindow - reservedOutputTokens` で計算し、必要な場合は tool result や巨大 JSON など機械的に短縮できる部分だけを圧縮します。圧縮後も超過する場合、API は `context_budget_exceeded` と `contextBudget` メタデータを返します。

## 認証

- `LOCAL_LLM_REQUIRE_AUTH=true` で Bearer 認証を有効化
- `LOCAL_LLM_ACCESS_TOKEN=<token>` を設定

## ライセンス

MIT
