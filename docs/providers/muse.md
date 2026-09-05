# Muse Agent Runtime 運用ガイド

## 状態

Muse Agent RuntimeのGateway実装とmock/MSP fixtureテストは利用可能です。実Muse subscriptionによるPhase 0検証は、この端末へMuse binaryを導入し、専用profileでログインしてから実施します。

Runtimeは初期状態で無効です。課金経路、schema fingerprint、provider/model、approval modeの検証証跡が一致しない限り、turnを開始しません。

## 1. Bridgeを構築する

```bash
pnpm --dir bridges/muse install --frozen-lockfile
pnpm run build:muse-bridge
pnpm run test:muse-bridge
```

`@muse-code/sdk`は`0.1.1`へ固定しています。Node.js 20以上が必要です。

## 2. Museを専用profileへ設定する

Muse Code公式手順でMuse binaryを導入してください。認証はlocal-llm専用のprofile rootを`HOME`として使用し、公式CLIの対話的ログイン手順をユーザー自身が実行します。

既存browser cookieの抽出や非公開tokenのコピーは行いません。通常のshellに設定されたPAYG API keyもMuse child processへ継承しません。

例:

```bash
export LOCAL_LLM_MUSE_PROFILE_ROOT="$HOME/.local/share/local-llm/muse-profile"
mkdir -p "$LOCAL_LLM_MUSE_PROFILE_ROOT"
chmod 700 "$LOCAL_LLM_MUSE_PROFILE_ROOT"
```

ログイン方法は利用中のMuse Codeリリースの公式案内に従ってください。

## 3. Phase 0証跡を作る

専用profileで次を実測します。

- `muse serve`のhandshakeが成功する
- schema fingerprint
- `model/list`が返すprovider IDとmodel ID
- strict policyへ割り当てるnative approval mode
- PAYG keyなしでsubscription turnが成功する
- subscription account側の利用記録
- approval deny、allow once、cancel、release/resume

確認結果を次のJSONとして、権限`0600`の通常ファイルへ保存します。symlinkは受け付けません。このファイルはcredentialではありませんが、Runtimeを有効化する判断根拠なのでGitへcommitしません。専用profile rootも通常ディレクトリかつ`0700`である必要があります。

schema version 1では下記フィールドを過不足なく指定し、`verified_at`にはUTC offset付きISO 8601日時を使用します。provider/model配列は空要素や重複を含められません。

```json
{
  "schema_version": 1,
  "runtime": "muse",
  "billing_mode": "subscription",
  "profile_root": "/absolute/path/to/muse-profile",
  "schema_fingerprint": "sha256:...",
  "provider_ids": ["verified-provider-id"],
  "model_ids": ["verified-model-id"],
  "approval_mode": "verified-native-mode",
  "verified_at": "2026-09-06T00:00:00Z"
}
```

`cost: null`や設定値だけをsubscription証明として使用しないでください。

## 4. Runtimeを有効化する

```bash
export LOCAL_LLM_MUSE_ENABLED=true
export LOCAL_LLM_MUSE_BINARY=/absolute/path/to/muse
export LOCAL_LLM_MUSE_PROFILE_ROOT=/absolute/path/to/muse-profile
export LOCAL_LLM_MUSE_BILLING_EVIDENCE_FILE=/absolute/path/to/muse-billing-evidence.json
export LOCAL_LLM_MUSE_SCHEMA_FINGERPRINT=sha256:...
export LOCAL_LLM_MUSE_ALLOWED_PROVIDER_IDS=verified-provider-id
export LOCAL_LLM_MUSE_ALLOWED_MODELS=verified-model-id
export LOCAL_LLM_MUSE_APPROVAL_MODE=verified-native-mode
export LOCAL_LLM_MUSE_APPROVAL_TIMEOUT_MS=300000
```

承認要求へこの時間内に回答がなければ、bridgeはMuseが提示したdeny choiceを選んで自動拒否します。deny choice自体が提示されない場合もallowへは進みません。

設定確認は外部turnを実行しません。

```bash
./.venv/bin/python scripts/check_muse_runtime.py
./.venv/bin/python scripts/check_muse_runtime.py --preflight
```

`--preflight`はMuse hostを起動してhandshakeしますが、turnは開始しません。

## 5. Agent API

主なendpoint:

```text
GET  /v1/agents/runtimes
POST /v1/agents/runtimes/muse/preflight
GET  /v1/agents/models?runtime=muse
POST /v1/agents/sessions
GET  /v1/agents/sessions/{session_id}
POST /v1/agents/sessions/{session_id}/resume
POST /v1/agents/sessions/{session_id}/release
POST /v1/agents/sessions/{session_id}/turns
POST /v1/agents/sessions/{session_id}/turns/{turn_id}/cancel
GET  /v1/agents/sessions/{session_id}/events
POST /v1/agents/sessions/{session_id}/approvals/{approval_id}/decision
POST /v1/agents/sessions/{session_id}/user-input/{user_input_id}/answer
```

状態を変更するrequestには`Idempotency-Key`が必要です。既存APIで認証が有効なら、Agent APIにも同じBearer tokenが必要です。

session状態は通常`idle` / `running`で、対話待ちは`waiting_for_approval`または`waiting_for_input`になります。これらのactive状態ではreleaseできません。Provider eventの欠落、model変更、approval mode変更、host異常終了を検出すると`recovery_required`へ遷移し、明示的resumeまで新規turnとevent streamを拒否します。

SSE購読にはsession単位の上限と購読者別queue上限があります。低速な購読者がqueue上限を超えた場合は接続を終了するため、clientは最後に処理した`Last-Event-ID`から再接続してください。GatewayはProviderのcursor pagingを使って欠落分を再構築します。

sessionをreleaseすると既存のSSE購読も終了します。Bridgeの出力待ちと未処理requestにも上限があり、呼び出し元が読み取れない状態ではmemoryを増やし続けず、retry可能なoverloadとして停止します。

`release`はProvider historyの削除ではありません。`view/unsubscribe`でGatewayの購読を解除し、後で明示的にresumeできます。

## 6. Live smoke

このtestはsubscription quotaを消費します。2つの環境変数を明示した場合だけ実行します。

```bash
RUN_LIVE_MUSE_TESTS=true \
ACK_MUSE_SUBSCRIPTION_USAGE=true \
./.venv/bin/python scripts/smoke_muse_agent.py
```

approval deny、allow once、cancelまで検証する場合:

```bash
RUN_LIVE_MUSE_TESTS=true \
ACK_MUSE_SUBSCRIPTION_USAGE=true \
./.venv/bin/python scripts/smoke_muse_agent.py --full
```

## 7. 停止と復旧

緊急停止は`LOCAL_LLM_MUSE_ENABLED=false`に戻してAPIを再起動します。既存の`/v1/models`、`/v1/chat/completions`、`/v1/responses`はMuse Runtimeから独立しています。

host crash後にturnを自動再送しません。session metadataと最後のcursorを確認し、明示的resumeを行ってください。fingerprintや課金証跡が現在の環境と一致しない場合は、再検証が終わるまで無効のままにします。

API process再起動後、releaseされていなかったsessionは`recovery_required`になります。`POST /v1/agents/sessions/{session_id}/resume`で明示的に復旧します。resume時にはworkspace、公開catalog上のmodel/provider、schema fingerprintが作成時と一致する必要があります。

## 公式資料

- [Muse Code SDK](https://github.com/meta-models/muse-code-sdk)
- [Muse SDK Quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/)
- [Sessions and turns](https://meta-models.github.io/muse-code-sdk/guides/msp-concepts/sessions-and-turns/)
- [view/unsubscribe](https://meta-models.github.io/muse-code-sdk/generated/msp/methods/view-unsubscribe/)
