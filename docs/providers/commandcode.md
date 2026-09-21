# Command Code DeepSeek APIプロキシ運用ガイド

## 概要

`local-llm`のOpenAI互換APIを入口にして、Command Code Provider APIの
DeepSeek V4.1 Flashを呼び出します。モデル重量をこのMacへ配置する構成ではありません。

```text
OpenAI互換クライアント
  -> http://127.0.0.1:44449/v1
  -> local-llm
  -> https://api.commandcode.ai/provider/v1
  -> deepseek/deepseek-v4.1-flash
```

local-llmで公開するモデル名は次です。

```text
commandcode/deepseek-v4.1-flash
```

上流へ送るときだけ、Command CodeのモデルID
`deepseek/deepseek-v4.1-flash`へ変換します。

## 前提条件

Command CodeのGoプランにはProvider APIアクセスがありません。GOAT、Pro、Max、
Team、またはProviderプランを使用してください。GoプランのCLI内部endpointを代理利用する
実装は含めていません。

必要なもの:

- Provider APIを利用できるCommand Codeプラン
- Command Code Studioで作成したAPI key
- Python依存関係を導入済みの`.venv`

公式資料:

- [Command Code Provider API](https://commandcode.ai/docs/provider)
- [Command Code Go Plan](https://commandcode.ai/docs/plans/go)

## 1. 認証情報を準備する

既存のCommand Code CLI認証を再利用する場合、次のファイルが利用できます。

```text
~/.commandcode/auth.json
```

通常ファイルかつ権限`0600`である必要があります。

```bash
chmod 600 ~/.commandcode/auth.json
```

API keyを環境変数から渡す場合は`COMMAND_CODE_API_KEY`を使用できます。ただし、shell履歴や
plistへ直接secretを書かないため、通常は認証ファイルの再利用を推奨します。

## 2. local-llmを設定する

`.env`へ次を設定します。

```dotenv
LOCAL_LLM_COMMANDCODE_ENABLED=true
LOCAL_LLM_COMMANDCODE_BASE_URL=https://api.commandcode.ai/provider/v1
LOCAL_LLM_COMMANDCODE_AUTH_FILE=/Users/your-name/.commandcode/auth.json
LOCAL_LLM_COMMANDCODE_TIMEOUT_SECONDS=300
```

Command Codeだけを使い、ローカルMLXモデルをロードしない場合:

```dotenv
LOCAL_INFERENCE_ENABLED=false
LOCAL_LLM_DAEMON_PRELOAD=false
```

外部端末へ公開する必要がなければ、待受先はloopbackに限定します。

```dotenv
GEMMA4_API_HOST=127.0.0.1
```

## 3. 依存関係を同期する

```bash
cd /Users/y.noguchi/Code/local-llm
uv pip sync requirements.lock --python .venv/bin/python
```

## 4. 課金なしで設定を確認する

静的確認:

```bash
./.venv/bin/python scripts/check_commandcode_provider.py
```

上流のモデル一覧まで確認:

```bash
./.venv/bin/python scripts/check_commandcode_provider.py --preflight
```

`preflight.status`が`ready`、`modelAvailable`が`true`なら、認証ファイルとモデル一覧取得は
正常です。この確認はモデル生成を実行しません。ただし、Goプランでもモデル一覧が取得できる
場合があるため、生成権限の最終確認には次のsmall smokeが必要です。

## 5. APIを起動または再起動する

LaunchAgentを利用する場合:

```bash
./scripts/launchd_autostart enable llm
./scripts/launchd_autostart status llm
```

foregroundで確認する場合:

```bash
./scripts/run_openai_api.sh
```

既定の`.env`では、この端末のAPI URLは次です。

```text
http://127.0.0.1:44449
```

## 6. local-llm側の状態を確認する

```bash
curl -sS http://127.0.0.1:44449/status | jq '.providers.commandcode'
curl -sS http://127.0.0.1:44449/v1/models | jq '.data[] | select(.owned_by == "command-code")'
```

期待するモデル:

```json
{
  "id": "commandcode/deepseek-v4.1-flash",
  "object": "model",
  "created": 0,
  "owned_by": "command-code"
}
```

local-llm認証を有効にしている場合は、上の`curl`へ
`-H "Authorization: Bearer ${LOCAL_LLM_ACCESS_TOKEN}"`を追加してください。

## 7. small smokeを実行する

次の呼び出しはCommand Codeのcreditsを少量消費します。

```bash
curl -sS http://127.0.0.1:44449/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "commandcode/deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
    "max_tokens": 16,
    "stream": false
  }' | jq
```

Responses API:

```bash
curl -sS http://127.0.0.1:44449/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "commandcode/deepseek-v4.1-flash",
    "input": "Reply with exactly: ready",
    "max_output_tokens": 16,
    "stream": false
  }' | jq
```

`403`と`upgrade_required`が返る場合は、API keyは有効でも現在のCommand Codeプランに
Provider API権限がありません。プラン変更後に同じ確認を再実行してください。

## 8. クライアントから利用する

OpenAI互換クライアントへ次を設定します。

```text
Base URL: http://127.0.0.1:44449/v1
Model:    commandcode/deepseek-v4.1-flash
API key:  local-llmで認証を有効にした場合のLOCAL_LLM_ACCESS_TOKEN
```

Command CodeのAPI keyをクライアントへ渡す必要はありません。local-llmだけが上流keyを読みます。

## 停止・切り戻し

Command Code経路だけを無効化する場合:

```dotenv
LOCAL_LLM_COMMANDCODE_ENABLED=false
```

設定後、APIを再起動します。Command Codeで障害やrate limitが発生しても、Museやローカルモデルへ
自動再送しません。意図しない二重課金やturn重複を防ぐためです。

APIサービス自体を停止する場合:

```bash
./scripts/launchd_autostart disable llm
```

## エラーの見方

- `commandcode_auth_required`: API keyまたは安全な認証ファイルが見つからない
- `commandcode_invalid_config`: base URLやtimeout設定が不正
- `commandcode_upstream_error`: DNS、TLS、timeoutなど上流通信の失敗
- `403 upgrade_required`: Command CodeプランにProvider API権限がない
- `429`: Command Code側のrate limitまたはcredit制限

上流のHTTP statusとJSON errorは可能な限りそのまま呼び出し元へ返します。API keyはresponseや
statusへ含めません。
