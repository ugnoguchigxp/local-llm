# Muse Agent Runtime 実装計画

作成日: 2026-09-06
状態: Gateway実装・mock検証済み / Phase 0実Muse検証未実施
上位文書: [local-llm Runtime Gateway コンセプト](runtime-gateway-concept.md)

## 1. 目的

Muse Code subscriptionを、`local-llm`の最初の外部Agent Runtimeとして追加する。

既存のローカル推論とOpenAI互換APIは変更せず、Muse専用のsession型Agent APIを並行して追加する。Museのtool execution、approval、user input、streaming、cancel、resumeを失わず、PAYGへ切り替わる可能性がある場合は実行しない。

この計画は、実装を独立して戻せる小さなフェーズへ分割し、各フェーズに変更対象、テスト、完了条件、停止条件を定める。

## 2. 実装前ベースライン

2026-09-06時点:

- API本体はPython/FastAPI。
- `/v1/models`、`/v1/chat/completions`、`/v1/responses`は各routeからlocal daemonを直接利用している。
- local daemonは単一worker queueとlocal model managerを持つ。
- tool executionはcaller責務であり、サーバーはtool callを返すだけである。
- Agent Runtime、session store、Agent APIは存在しない。
- root `package.json`はlaunchd操作用scriptだけを持ち、Node依存はまだない。
- Node.js `v24.11.1`、pnpm `10.24.0`を利用可能。
- `@muse-code/sdk`の公開最新版は確認時点で`0.1.1`。
- `muse` binaryは未インストール。
- 既存の主要API関連テスト48件は成功済み。root `tests/`には57件のtest functionがある。
- worktreeには本計画と無関係な既存変更があるため、それらへ触れない。

実装開始時に、次の基準を改めて記録する。

```bash
./.venv/bin/python -m pytest tests -q
git status --short
node --version
pnpm --version
npm view @muse-code/sdk version --json
```

全test実行が環境依存で失敗する場合は、失敗理由と対象を記録し、少なくとも以下を回帰基準とする。

```bash
./.venv/bin/python -m pytest \
  tests/test_chat_route.py \
  tests/test_responses_route.py \
  tests/test_tool_calling.py \
  tests/test_context_budget.py \
  tests/test_model_generation.py -q
```

## 3. スコープ

### 含める

- Muse Code SDKを使用するNode.js bridge
- Pythonからbridgeを扱う非同期client
- Muse用`AgentRuntime` adapter
- Agent Runtime catalog
- sessionの開始、取得、再開、release
- text turnの開始
- cursor付きSSE event stream
- approval要求と`allow_once` / `deny`
- user input要求への回答
- turn cancel
- session metadataのSQLite永続化
- isolated workspace
- dedicated Muse profile
- subscription-only検証
- health/status
- mock testとopt-in live smoke
- 設定、運用、トラブルシューティング文書

### 最初のリリースに含めない

- Grok、QwenCloud、Kimi等の実装
- `/v1/chat/completions`や`/v1/responses`からのMuse利用
- `/v1/models`へのMuse表示
- Provider間routingとfallback
- custom toolの登録
- caller toolとMuse toolの相互変換
- image、video、PDF等のattachment
- persistent approval
- approval無効化、`yolo`相当
- 複数Uvicorn worker
- 外部公開用のmulti-user認可
- subscription購入、login、credential更新を行うAPI
- quota残量の推測

## 4. 実装方針

### 4.1 既存APIから分離する

Museのrouteは新しい`api/routes/agents.py`へ追加し、既存3 routeの処理経路は変更しない。

```text
FastAPI
├── existing Model API
│   ├── /v1/models
│   ├── /v1/chat/completions
│   └── /v1/responses
│       └── existing local daemon
│
└── new Agent API
    └── /v1/agents/*
        └── AgentService
            └── MuseRuntime
                └── Muse Bridge
                    └── @muse-code/sdk
                        └── muse serve
```

Museがdisabled、未インストール、未ログイン、異常終了のいずれでも、既存Model APIは起動・利用できる状態を維持する。

### 4.2 Node bridgeを薄く保つ

bridgeはMSPと内部NDJSONの変換だけを担当する。HTTP、SQLite、workspaceの許可判断、上位routingをNode側へ持ち込まない。

Python側はMSPを再実装せず、sessionやnotificationの正しさを公式SDKへ委ねる。型が提供されるpayloadは`any`へ落とさず、open setとして届くnotification paramsはruntime validationでnarrowingする。未検証のcastで通さない。

TypeScriptが必要なのはこのbridgeだけである。公開されているMuse SDKがTypeScript/Node.js向けであり、fingerprint検証、command idempotency、server request、host shutdownを公式実装へ委ねるために採用する。FastAPI、catalog、state、workspace、認証、billing gateはPythonへ統一し、将来公式Python SDKまたは安定したMSP仕様が提供された場合はbridgeだけを差し替えられる境界にする。

SDK内ではnotification handlerを所有する箇所を1つに限定する。Phase 0で、高水準の`MuseClient`だけで`model/list`、`view/page`、`view/unsubscribe`、raw notificationをすべて扱えるかを確認する。不足する場合はSDKが公開するtyped `Connection`とschema型を使用し、MSPのwire framing自体は再実装しない。`MuseClient`と低水準connectionの双方からhandlerを登録する構成にはしない。

### 4.3 外部APIにはnative IDを直接公開しない

callerへ返すのはgateway session IDとgateway turn IDとする。Muse session ID、turn ID、cursorは内部stateで対応付ける。

modelだけはcatalogから得たnative model IDを`muse/<native-model-id>`として公開する。hardcodeした最新モデルaliasは作らない。

### 4.4 subscription-onlyを機能フラグにしない

Muse v1にはPAYG fallbackの実装自体を入れない。subscription経路が確認できない場合は`runtime_billing_unverified`とし、turnを開始しない。

## 5. 予定ファイル

```text
agent_runtime/
├── __init__.py
├── base.py
├── catalog.py
├── errors.py
├── events.py
├── registry.py
├── service.py
├── state.py
├── workspaces.py
└── muse/
    ├── __init__.py
    ├── bridge_client.py
    ├── config.py
    ├── error_mapping.py
    └── runtime.py

bridges/
└── muse/
    ├── package.json
    ├── tsconfig.json
    ├── src/
    │   ├── main.ts
    │   ├── protocol.ts
    │   ├── event_mapper.ts
    │   ├── billing_evidence.ts
    │   └── redact.ts
    └── tests/
        ├── protocol.test.ts
        ├── event_mapper.test.ts
        ├── lifecycle.test.ts
        └── redact.test.ts

api/
├── agent_schemas.py
└── routes/
    └── agents.py

tests/
├── fixtures/
│   └── muse_bridge/
├── test_agent_routes.py
├── test_agent_service.py
├── test_agent_state.py
├── test_agent_workspaces.py
├── test_muse_bridge_client.py
├── test_muse_error_mapping.py
└── test_muse_runtime.py

scripts/
├── check_muse_runtime.py
└── smoke_muse_agent.py

docs/
└── providers/
    └── muse.md
```

実装中に責務の薄いファイルが生じる場合は統合してよい。ディレクトリ構造を守ることより、依存方向を守ることを優先する。

## 6. 依存方向

```text
api/routes/agents.py
        │
        ▼
agent_runtime/service.py
        │
        ├── state.py
        ├── workspaces.py
        └── AgentRuntime protocol
                   │
                   ▼
          muse/runtime.py
                   │
                   ▼
          muse/bridge_client.py
                   │
                   ▼
          Node Muse Bridge
```

禁止する依存:

- Node bridgeからFastAPIやSQLiteへの依存
- `api/`からMuse SDK固有型への依存
- Muse Runtimeから既存local daemonへの依存
- 既存chat/responses routeからAgentServiceへの依存
- provider error stringをroute内で直接判定する処理

## 7. Public Agent API契約

正確なPydantic schemaはPhase 1で固定する。最小形は次のとおりとする。

### 7.1 Runtime一覧

```http
GET /v1/agents/runtimes
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "muse",
      "status": "disabled",
      "billing_mode": "unknown",
      "auth": "unknown",
      "protocol_fingerprint": null,
      "active_sessions": 0,
      "active_turns": 0
    }
  ]
}
```

このendpointはbridgeを暗黙に起動しない。最後に確認した状態を返し、必要なら明示的preflight処理を別途行う。

### 7.2 Agent model一覧

```http
GET /v1/agents/models?runtime=muse
```

利用可能性とsubscription検証に成功したmodelだけを返す。未検証時は空配列とruntime状態を返し、静的な架空modelを返さない。

### 7.3 Session作成

```http
POST /v1/agents/sessions
Idempotency-Key: <caller-generated-key>
```

```json
{
  "runtime": "muse",
  "model": "muse/<native-model-id>",
  "approval_policy": "strict",
  "workspace": {
    "mode": "isolated"
  }
}
```

初回リリースで受け付ける`approval_policy`は`strict`だけとする。Provider固有のapproval mode名はMuse adapter内で、Phase 0で確認した最も制限の強い対話可能なmodeへ変換する。

成功時は`201 Created`を返す。

```json
{
  "id": "ags_...",
  "runtime": "muse",
  "model": "muse/<native-model-id>",
  "status": "idle",
  "workspace": {
    "mode": "isolated"
  },
  "events_url": "/v1/agents/sessions/ags_.../events"
}
```

### 7.4 Session取得・resume・release

```http
GET  /v1/agents/sessions/{session_id}
POST /v1/agents/sessions/{session_id}/resume
POST /v1/agents/sessions/{session_id}/release
```

- GETはgateway metadataと現在の状態だけを返し、promptやresponse本文を返さない。
- resumeはidempotency keyを要求し、Provider sessionのwriter leaseとevent viewを取得し直す。
- releaseはgateway側のsubscriptionを解除して`released`へ遷移させる。native historyの削除APIではない。
- released sessionへの新規turnは409とし、明示的resumeが成功するまで受け付けない。

### 7.5 Turn開始

```http
POST /v1/agents/sessions/{session_id}/turns
Idempotency-Key: <caller-generated-key>
```

```json
{
  "input": [
    {
      "type": "text",
      "text": "Implement the requested change and run the tests."
    }
  ]
}
```

最初はtext partだけを受け付ける。成功時はProviderの完了を待たず`202 Accepted`を返す。

```json
{
  "id": "agt_...",
  "session_id": "ags_...",
  "status": "accepted",
  "events_url": "/v1/agents/sessions/ags_.../events"
}
```

### 7.6 Event stream

```http
GET /v1/agents/sessions/{session_id}/events?after=<opaque-cursor>
Accept: text/event-stream
```

```text
id: <opaque-cursor>
event: message.delta
data: {"session_id":"ags_...","turn_id":"agt_...","data":{"text":"..."}}
```

- `id`をSSE cursorとして使う。
- 再接続時はquery `after`または`Last-Event-ID`を受け付ける。
- Providerがcursor消失をerrorとして返した場合は`event_cursor_expired`を返す。paging中に`view/gap`が通知された場合は、安全に確認できたcursorを維持してsessionを`recovery_required`へ移す。
- heartbeat commentを定期送信し、proxy idle timeoutを避ける。
- client切断だけではturnをcancelしない。
- `turn.completed`、`turn.cancelled`、`turn.failed`、`turn.unqueued`をterminal eventとする。
- gatewayは1 session 1 active turnを強制するため、通常はnative queueを作らない。それでもraceやresume後に`turn/unqueued`を受け取った場合は、完了待ちを続けず`turn.unqueued`へ正規化する。

### 7.7 Approval

`approval.requested` eventに、Providerが提示した選択肢を安全な共通形で含める。

```http
POST /v1/agents/sessions/{session_id}/approvals/{approval_id}/decision
```

```json
{
  "decision": "allow_once"
}
```

最初に許可するdecision:

- `allow_once`
- `deny`

workspace/sessionへ永続するapprovalは受け付けない。Providerが`allow_once`を提示しない場合はallowできず、denyのみとする。

### 7.8 User input

```http
POST /v1/agents/sessions/{session_id}/user-input/{request_id}/answer
```

```json
{
  "answers": [
    {
      "question_id": "question-1",
      "free_text": "Proceed with option A."
    }
  ]
}
```

全questionへ1件ずつ回答する。各回答は`selected_label`、`selected_labels`、`free_text`のいずれか1つだけを持ち、Providerから受信したlabelとquestion IDを使用する。

### 7.9 Cancel、resume、release

- cancelは`202`を返し、完了は`turn.cancelled`で通知する。
- resumeはProvider sessionのwriter leaseを取得し直し、cursorからstateを再構築する。
- releaseはgatewayのsubscriptionを`view/unsubscribe`相当で解除し、idle状態になったnative sessionのwriter lease解放をMuse hostへ委ねる。
- Muse v1にはsession削除やsession単位のnative `close`を想定しない。Providerのdurable historyは削除せず、gateway metadataを`released`へ遷移させる。
- deleteはProvider側の正式な削除契約が確認できるまで実装しない。

## 8. Error API契約

Agent APIでは、既存routeのエラー形を変更せず、新規route内だけで次の形式を使用する。

```json
{
  "error": {
    "code": "runtime_billing_unverified",
    "message": "Muse subscription billing could not be verified.",
    "runtime": "muse",
    "retryable": false,
    "retry_after": null,
    "request_id": "req_..."
  }
}
```

| HTTP | code | 用途 |
| ---: | --- | --- |
| 400 | `invalid_agent_request` | schema外ではなく意味上不正な入力 |
| 400 | `unsupported_capability` | attachment等の未対応機能 |
| 403 | `workspace_not_allowed` | 許可root外のworkspace |
| 404 | `agent_model_not_found` | catalogにないmodel |
| 404 | `session_not_found` | gateway sessionなし |
| 409 | `session_in_use` | session writer競合 |
| 409 | `turn_in_progress` | 同一sessionのactive turn競合 |
| 409 | `interaction_already_resolved` | approval/inputの二重回答 |
| 429 | `provider_subscription_exhausted` | subscription quota超過 |
| 429 | `provider_rate_limited` | 一時rate limit |
| 503 | `runtime_disabled` | 設定上無効 |
| 503 | `runtime_unavailable` | binary/bridge/host利用不能 |
| 503 | `runtime_auth_required` | Muse loginが必要 |
| 503 | `runtime_billing_unverified` | 課金経路を確認不能 |
| 503 | `provider_protocol_mismatch` | 安全に扱えないschema差異 |
| 503 | `provider_host_exited` | host異常終了 |

Providerのraw error、stderr、stack traceをresponseへそのまま含めない。

## 9. Python–Node内部protocol

標準入出力を使うversion付きNDJSONとする。Pythonは`asyncio.create_subprocess_exec`を使用し、`shell=True`は使わない。

### Request

```json
{
  "v": 1,
  "id": "brq_...",
  "method": "session.start",
  "params": {}
}
```

### Response

```json
{
  "v": 1,
  "id": "brq_...",
  "ok": true,
  "result": {}
}
```

```json
{
  "v": 1,
  "id": "brq_...",
  "ok": false,
  "error": {
    "kind": "auth_required",
    "message": "redacted message",
    "retryable": false,
    "provider_data": {}
  }
}
```

### Event

```json
{
  "v": 1,
  "event": true,
  "type": "message.delta",
  "native_session_id": "...",
  "native_turn_id": "...",
  "native_cursor": "...",
  "data": {}
}
```

実装するmethod:

- `runtime.initialize`
- `runtime.health`
- `models.list`
- `session.start`
- `session.resume`
- `session.release`
- `turn.start`
- `turn.cancel`
- `approval.decide`
- `user_input.answer`
- `events.page`
- `runtime.shutdown`

`session.release`はbridge内部のGateway methodであり、nativeな`session/close`を呼ぶものではない。現在のview subscriptionを`view/unsubscribe`で解除し、保持中のsession objectとevent routing stateを破棄する。

protocol要件:

- 不明なversion、method、fieldを明示的に拒否する。
- request IDごとにPromiseを対応付ける。
- state変更methodはidempotency keyを必須にする。
- stdoutにはprotocol frame以外を出さない。
- diagnosticはstderrへ出し、redactと長さ制限を適用する。
- 1 frameと内部bufferへ上限を設ける。
- 不正frame時はbridge全体を即座に壊さず、対象requestを失敗させる。
- stdout EOF時は全pending requestを`provider_host_exited`で失敗させる。
- backpressure時は無制限にeventをmemoryへ蓄積しない。

## 10. Session永続化

Python標準`sqlite3`を使い、外部依存を増やさない。DBはlocal-llm data root配下へ保存する。

最小table:

### `agent_sessions`

- `gateway_session_id` primary key
- `runtime_id`
- `native_session_id`
- `public_model_id`
- `native_model_id`
- `provider_id`
- `workspace_mode`
- `workspace_path`
- `approval_policy`
- `last_native_cursor`
- `status`
- `protocol_fingerprint`
- `created_at`
- `updated_at`
- `released_at`

### `agent_idempotency`

- `scope`
- `idempotency_key`
- `operation`
- `request_hash`
- `gateway_resource_id`
- `native_command_id`
- `status`
- `created_at`
- `expires_at`

同じkeyで異なるrequest bodyが送られた場合は409を返す。

保存しないもの:

- credential
- prompt本文
- response本文
- tool argument本文
- approval対象のraw command
- Provider event全文

SQLite migrationはschema versionを持ち、API起動時に後方互換なmigrationだけを適用する。migration失敗で既存Model APIを停止させず、Muse Runtimeだけを`degraded`にする。

## 11. Cursorとevent replay

- Providerのnative cursorは外部へ直接返さない。
- gateway cursorはversion、session、runtime、native cursorへの参照を持つopaque tokenとする。
- token改変を検出できる形式にする。
- event本文はSQLiteへ永続化せず、MSPのresume/view pagingから再構築する。
- replay時も同じnormalized event mapperを通す。
- protocolが要求期間のreplayをerrorとして拒否した場合は`event_cursor_expired`を返す。欠落範囲を示す`view/gap`の場合は自動的に飛ばさず、`session.recovery_required` eventを返して明示的resumeを要求する。
- API再起動後は明示的なresumeを先に要求し、成功後のSSE接続で最後に確認したcursorからreplayを開始する。

cursor設計でProvider event本文の保存が避けられないと判明した場合は、retention、暗号化、最大容量を別の設計判断として承認するまで実装を止める。

## 12. Workspaceとapprovalの安全要件

### Workspace

- default rootはlocal-llm data root配下とする。
- gateway session IDごとに新しいdirectoryを作る。
- modeは最初は`isolated`だけを公開する。
- 将来の`allowed_path`用にresolverは分離するが、最初のAPIでは受け付けない。
- pathは`resolve()`後に許可root内であることを確認する。
- symlink経由のescapeをtestする。
- session release時に即削除しない。retention policy確定までは明示的cleanupだけとする。

### Approval

- public policyは`strict`のみ。
- Phase 0で確認したnative modeへ明示的にmapする。
- native defaultへ暗黙に任せない。
- persistent approval choiceはAPIから除外する。
- approval timeoutはdenyとして扱う。
- approval requestに含まれるpath、command、URLは表示用データとして扱い、実行指示として解釈しない。
- gateway authを通過したcallerだけがdecisionを送れる。

### Child process

- binary pathは起動前にabsolute pathへ解決する。
- bridgeと`muse`はargv配列でspawnする。
- 子プロセス環境はallowlistから構築する。
- shell全体のhomeを変更せず、Muse childだけに専用profile rootを渡す。
- `META_API_KEY`などPAYG key候補を継承しない。
- proxy、certificate、locale等は必要性を確認して個別に許可する。

## 13. Billing verification

### Phase 0で集めるevidence

- Muse binaryの絶対pathとversion
- SDK version
- MSP schema fingerprint
- provider ID
- native model ID
- model catalog sourceとcost metadata
- dedicated profileの認証方式
- PAYG API keyがchild environmentにないこと
- subscription account側の利用記録
- turn成功時とquota超過時のredact済みnative error

`cost`が`null`であることだけをsubscription判定に使わない。逆にtoken単価が表示されることだけでPAYGと断定しない。公式仕様で意味を確認する。

### Runtime判定

```text
disabled
  └─ enabled config
       └─ binary + SDK + handshake
            └─ auth verified
                 └─ billing provenance verified
                      └─ provider/model allowlist match
                           └─ ready
```

どこかが不明なら`ready`にしない。手動で`ready`を強制するbypassは最初のリリースへ入れない。

## 14. 設定案

Muse固有設定は`LOCAL_LLM_MUSE_`、Agent Runtime共通の永続化設定は`LOCAL_LLM_AGENT_` prefixとする。

| 設定 | 初期値 | 用途 |
| --- | --- | --- |
| `LOCAL_LLM_MUSE_ENABLED` | `false` | Muse Runtime有効化 |
| `LOCAL_LLM_MUSE_BINARY` | `muse` | 公式binary path |
| `LOCAL_LLM_NODE_BINARY` | `node` | bridgeを実行するNode.js binary |
| `LOCAL_LLM_MUSE_BRIDGE_ENTRY` | repository内のbuild成果物 | bridge entrypoint |
| `LOCAL_LLM_MUSE_PROFILE_ROOT` | なし | 専用profile root。enabled時必須 |
| `LOCAL_LLM_MUSE_WORKSPACE_ROOT` | data root配下 | isolated workspace root |
| `LOCAL_LLM_MUSE_BILLING_EVIDENCE_FILE` | なし | Phase 0で作成するsubscription検証証跡 |
| `LOCAL_LLM_MUSE_ALLOWED_MODELS` | なし | 公開を許可するnative model ID |
| `LOCAL_LLM_MUSE_ALLOWED_PROVIDER_IDS` | なし | 許可provider ID |
| `LOCAL_LLM_MUSE_SCHEMA_FINGERPRINT` | なし | 検証済みfingerprint |
| `LOCAL_LLM_MUSE_APPROVAL_MODE` | なし | `strict`に対応付ける検証済みnative mode |
| `LOCAL_LLM_MUSE_MAX_SESSIONS` | `2` | 同時loaded session上限 |
| `LOCAL_LLM_MUSE_STARTUP_TIMEOUT_MS` | `10000` | bridge/host handshake timeout |
| `LOCAL_LLM_MUSE_REQUEST_TIMEOUT_MS` | `30000` | bridge request timeout |
| `LOCAL_LLM_MUSE_SHUTDOWN_TIMEOUT_MS` | `30000` | Muse host graceful shutdown timeout |
| `LOCAL_LLM_MUSE_APPROVAL_TIMEOUT_MS` | `300000` | approval待機上限 |
| `LOCAL_LLM_MUSE_DEBUG_LOG` | `false` | redact済みdebug log |
| `LOCAL_LLM_AGENT_STATE_DB` | data root配下 | session/idempotency metadata用SQLite |
| `LOCAL_LLM_AGENT_CURSOR_SECRET_FILE` | data root配下 | 公開SSE cursor署名鍵 |

allowlistが空のとき全許可とは解釈しない。enabled時に必須設定が欠けていれば`runtime_billing_unverified`または`runtime_unavailable`とする。

初回実装ではsession解放は明示的`release`とprocess shutdownで行う。自動idle TTLは、実Museでwriter leaseと長時間turnの挙動を測定した後に追加する。

live test用設定は通常Runtime設定から分離する。

```text
RUN_LIVE_MUSE_TESTS=false
ACK_MUSE_SUBSCRIPTION_USAGE=false
```

両方が明示的にtrueの場合だけ、quotaを消費するtestを実行する。

## 15. App lifecycle

- module import時にbridgeをspawnしない。
- Muse disabled時はNode dependencyや`muse` binaryを要求しない。
- enabled時もFastAPI全体のstartupを失敗させない。
- AgentServiceはlazy initializationを基本とする。
- preflightは最初のMuse API requestまたは明示的check scriptで実行する。
- process内でMuse bridgeを1つだけ管理する。
- shutdownでは新規turn受付を停止し、active turnを短時間drainした後、bridgeをgraceful closeする。
- timeout後はprocessを段階的にterminateする。
- host crash後にturnを自動再送しない。
- restart後はsessionを自動実行せず、明示的resumeで復旧する。

## 16. フェーズ別実装

### Phase 0: Feasibility gate

目的:

Muse subscription経路、SDK/MSP、approval、resumeが要求どおり使えるかを、製品コードへ組み込む前に確認する。

作業:

1. 公式手順でMuse binaryをインストールする。
2. local-llm専用profile rootを用意し、ユーザー操作でloginする。
3. SDK versionを再確認してexact pin候補を決める。
4. 一時的な外部spikeで`muse serve`を起動する。
5. `initialize`、fingerprint、`model/list`を記録する。
6. `MuseClient` facadeとtyped `Connection`のどちらをbridge境界にするか決める。`model/list`、`view/page`、`view/unsubscribe`、notification購読を1つのhandler所有者で扱えることを条件とする。
7. PAYG keyを渡さず、isolated temporary workspaceでtext turnを1回実行する。
8. approval要求、deny、allow onceをそれぞれ確認する。
9. turn cancel、session release、session resumeを確認する。
10. queued turnが発生しないようgateway側で直列化しつつ、fixtureで`turn/unqueued`のterminal処理を確認する。
11. host processを意図的に終了し、exit分類を確認する。
12. subscription利用記録と課金経路を確認する。

成果物:

- redact済み検証記録
- 使用SDK versionとschema fingerprint
- 採用するSDK抽象化レベルとnotification handler所有方針
- allowlist候補
- native error fixture
- Go / No-Go判断

完了条件:

- subscription経路を合理的に証明できる。
- PAYG credentialなしでturnが成功する。
- `strict`へmapできるnative approval modeがある。
- approval、cancel、resume、host exitを観測できる。

停止条件:

- 課金経路を区別できない。
- SDK経由がsubscription対象外。
- headless利用にapproval無効化が必要。
- workspace境界が強制されない。
- `muse serve`が利用できない。

ロールバック:

- spikeとtemporary workspaceだけを削除する。
- Muse profile/credentialは自動削除せず、ユーザーへ場所を報告する。
- 製品コードへ変更を入れない。

### Phase 1: Contract-first skeleton

対象:

- `agent_runtime/base.py`
- `agent_runtime/events.py`
- `agent_runtime/errors.py`
- `api/agent_schemas.py`
- `tests/test_agent_routes.py`
- `tests/test_agent_service.py`

作業:

1. public request/response/event/error schemaを定義する。
2. `AgentRuntime` Protocolを定義する。
3. in-memory fake runtimeをtest内に作る。
4. route shape、status code、auth適用を先にtestで固定する。
5. Muse固有型がpublic schemaへ漏れていないことを確認する。

完了条件:

- fake runtimeだけでAgent API contract testが通る。
- 既存routeとschemaを変更していない。
- Muse disabled時のresponseが固定される。

停止条件:

- Agent API追加のために既存OpenAI schemaを破壊する必要が生じる。
- approvalまたはSSEの契約が決まらない。

ロールバック:

- 新規moduleとrouter登録だけをrevertできる単位にする。

### Phase 2: Node Muse bridge

対象:

- `bridges/muse/**`
- rootまたはbridge packageのNode lockfile

作業:

1. `@muse-code/sdk`をPhase 0で検証したexact versionへ固定する。
2. TypeScript strict modeを有効にする。
3. NDJSON parser、request correlation、event writerを実装する。
4. SDK handshake、catalog、session、turn、approval、input、cancel、release、resumeを接続する。
5. schema fingerprint checkを実装する。
6. stderr redactionとframe size上限を実装する。
7. fake SDK transportまたは公式transcriptでunit testする。
8. stdoutへprotocol以外が出ないことをtestする。

検証:

```bash
pnpm run build:muse-bridge
pnpm run test:muse-bridge
```

完了条件:

- 実Muse credentialなしでNode unit testが通る。
- SDK型に対する不要な`any`がなく、open setのnotification payloadをruntime validationしている。
- notificationを取りこぼす前に、唯一のownerがhandlerを登録する。
- host exitですべてのpending requestが失敗する。

停止条件:

- SDKが必要な操作を公開していない。
- fingerprint差異を検出できない。
- event replayに必要なcursorを取得できない。

ロールバック:

- bridge packageを削除すればPython側へ影響しない状態を維持する。

### Phase 3: Python bridge clientとMuseRuntime

対象:

- `agent_runtime/muse/**`
- `tests/test_muse_bridge_client.py`
- `tests/test_muse_runtime.py`
- `tests/test_muse_error_mapping.py`

作業:

1. async subprocess supervisionを実装する。
2. request IDとFutureの対応を実装する。
3. event readerとbounded queueを実装する。
4. bridge errorを`AgentRuntimeError`へmapする。
5. Muse native IDとgateway IDの変換境界を作る。
6. billing evidenceとallowlist gateを実装する。
7. fake bridge subprocessを使ったtestを追加する。
8. malformed frame、timeout、EOF、stderr redactionをtestする。

完了条件:

- Python testからfake bridgeをspawnし、sessionとturnを完了できる。
- process leakと未回収taskがない。
- billing未検証時にturnを開始しない。
- host crash後にrequestを自動再送しない。

停止条件:

- event backpressureをboundedにできない。
- subprocess shutdownが安定しない。
- native errorから安全な共通codeへmapできない。

ロールバック:

- Runtime登録を無効にすればbridge clientが起動しない構造を維持する。

### Phase 4: Stateとworkspace

対象:

- `agent_runtime/state.py`
- `agent_runtime/workspaces.py`
- `tests/test_agent_state.py`
- `tests/test_agent_workspaces.py`

作業:

1. SQLite schemaとmigrationを実装する。
2. gateway/native ID mappingを保存する。
3. idempotency keyとrequest hashを保存する。
4. isolated workspace作成を実装する。
5. path canonicalizationとsymlink escape防止を実装する。
6. resume前のworkspace/model/fingerprint再検証を実装する。
7. DB破損・migration失敗時にMuseだけをdegradedにする。

完了条件:

- API process再生成後にfake sessionをresumeできる。
- 同じidempotency keyでresourceが重複生成されない。
- 異なるrequest bodyで同じkeyを使うと409になる。
- 許可root外とsymlink escapeが拒否される。
- transcriptやcredentialがDBに保存されない。

停止条件:

- resumeのためにprompt/response本文の保存が必要になる。
- Provider cursorを安全に再利用できない。

ロールバック:

- migrationは既存tableを変更せず、新規DBだけを対象にする。
- Muse disabled時はDBを開かない。

### Phase 5: AgentService、routes、SSE

対象:

- `agent_runtime/registry.py`
- `agent_runtime/service.py`
- `agent_runtime/catalog.py`
- `api/routes/agents.py`
- `api/main.py`
- `tests/test_agent_routes.py`
- `tests/test_agent_service.py`

作業:

1. Runtime registryへMuseを登録する。
2. session/turn orchestrationをAgentServiceへ実装する。
3. existing API authを全Agent routeへ適用する。
4. Agent専用model catalogを実装する。
5. SSE mapper、cursor、heartbeat、reconnectを実装する。
6. approval/user input/cancelを実装する。
7. route内のMuse分岐を禁止し、Runtime Protocol経由にする。
8. app lifespanへnon-fatal shutdown hookを追加する。

完了条件:

- fake Muse Runtimeで全public API testが通る。
- SSE再接続でevent重複・欠落がない。
- client切断でturnがcancelされない。
- approval timeoutがallowにならない。
- Muse障害中も既存Model API testが通る。

停止条件:

- AgentServiceの追加でlocal daemon lifecycleが変わる。
- `/v1/models`や既存request schemaの変更が必要になる。

ロールバック:

- router登録とlifespan hookを外せば既存APIだけへ戻る。

### Phase 6: Status、運用、live smoke

対象:

- `api/main.py`
- `scripts/check_muse_runtime.py`
- `scripts/smoke_muse_agent.py`
- `docs/providers/muse.md`
- `README.md`
- `.env.example`が存在する場合は同ファイル

作業:

1. `/status`へMuseの非機密状態を追加する。
2. install/loginを行わないread-only check scriptを追加する。
3. 明示的opt-inのlive smokeを追加する。
4. quota/auth/protocol/host exitの実error fixtureをtestへ反映する。
5. 設定、専用profile、workspace、approval、復旧手順を書く。
6. 通常ログにsecretやpromptがないことを監査する。
7. SDK/packageのlicenseと配布方法を確認する。

live検証:

```bash
RUN_LIVE_MUSE_TESTS=true \
ACK_MUSE_SUBSCRIPTION_USAGE=true \
./.venv/bin/python scripts/smoke_muse_agent.py
```

smoke内容:

- runtime ready
- model catalog
- isolated session
- text turnとdelta
- approval deny
- approval allow once
- cancel
- release/resume
- host restart recovery
- forbidden workspace
- PAYG key非継承

完了条件:

- live smokeがMuse subscription経路で成功する。
- subscription使用以外の課金記録が発生しない。
- Museの有無に関係なく既存APIが回帰しない。
- docsだけでsetup、確認、停止、復旧ができる。

停止条件:

- liveとmockでerror semanticsが一致しない。
- subscription quota errorを安全に識別できない。
- 通常ログへ機密情報が出る。

ロールバック:

- `LOCAL_LLM_MUSE_ENABLED=false`で完全に停止できる。
- bridge/host processが残らないことを確認する。

## 17. Test matrix

### Python unit/API

- Runtime disabled/unavailable/auth required/billing unverified
- catalog allowlist
- model not found
- session create idempotency
- one active turn per session
- message deltaとterminal event
- approval allow once/deny/duplicate/timeout
- user input free text/choice/invalid choice
- cancel accepted/terminal
- resume after process restart
- cursor replay/expired/tampered
- workspace traversal/symlink escape
- bridge timeout/malformed frame/EOF
- secrets masking
- SQLite migration failure isolation
- API auth適用

### Node bridge

- handshake前のcommand拒否
- notification handler登録順
- request/response correlation
- native command idempotency
- item delta/completed fold
- approval request/decision
- user input request/answer
- cancel terminal待機
- resume/view paging
- release/view unsubscribe
- `turn/unqueued`のterminal処理
- fingerprint mismatch
- unknown open-set value
- stdout purity
- stderr redaction
- frame/buffer limit
- host exit classification

### Regression

```bash
./.venv/bin/python -m pytest tests -q
pnpm run build:muse-bridge
pnpm run test:muse-bridge
git diff --check
```

### Live

通常CIでは実行しない。専用profileと明示的ackがあるローカル環境だけで実行する。

## 18. Observability

記録する:

- runtime状態遷移
- bridge/host start、ready、exit
- SDK version、host version、fingerprint
- request/session/turnのgateway ID
- latency
- event数
- token usage
- approval待機時間
- quota/rate limit code
- active session/turn数

記録しない:

- credential
- Authorization header
- profile file内容
- prompt/response本文
- tool argument本文
- raw approval command

ログeventはJSONとし、`event`、`runtime`、`request_id`、`session_id`、`turn_id`を可能な範囲で揃える。

## 19. Performanceとresource limit

- bridgeは常駐1 processを基本とする。
- `muse serve`もbridgeが所有する1 processを基本とする。
- 同時loaded sessionは初期値2。
- 1 session 1 active turn。
- SSE subscriber数とevent queueに上限を設ける。
- 明示的にreleaseしたsessionはwriter購読を解放するが、Provider historyは削除しない。
- Gateway再起動時の未release sessionは`recovery_required`とし、明示的resume時にworkspace、model/provider、fingerprintを再検証する。
- stderr、event data、user inputへ個別のsize上限を設ける。
- API server shutdown時間へ上限を設ける。

実測前に高い並列性を約束しない。NightWorkersでの大量実行は、Phase 6の計測後に別途concurrency設計を行う。

## 20. 変更バッチとロールバック単位

1. Contract/API skeleton
2. Node bridge
3. Python bridge client/MuseRuntime
4. State/workspace
5. AgentService/routes/SSE
6. Status/docs/live fixtures

各バッチは単独でtest可能にし、次のバッチへ進む前に既存Model APIの回帰確認を行う。未完成のMuse Runtimeはdefault disabledとし、途中状態でもmain branch上の既存機能を壊さない。

## 21. 実装完了判定

以下をすべて満たしたとき、Muse初回実装を完了とする。

- Phase 0がGo判定。
- Museはdefault disabled。
- Muse未導入環境で既存APIとtestが成功。
- Muse有効環境でsubscription経路を検証できる。
- PAYG keyをchildへ渡さない。
- Agent専用catalogから実在modelだけを返す。
- isolated sessionを作成できる。
- text turnを非同期開始できる。
- SSEでdelta、interaction、terminalを受信できる。
- approvalはstrictかつallow once/denyのみ。
- user inputへ回答できる。
- cancel、release、resumeが機能する。
- API再起動後にsession metadataからresumeできる。
- host crashを検出し、自動再送しない。
- quota超過を429へ正規化できる。
- secrets、prompt本文、tool argumentsを通常ログやSQLiteへ保存しない。
- mock test、regression test、opt-in live smokeが成功。
- READMEとMuse運用文書が実装と一致する。

## 22. 実装後に検討するもの

Museがreference implementationとして安定した後、次の順に検討する。

1. 実クライアントから必要になったattachment対応
2. 安全性を検証したallowed-path workspace
3. Grok系Agent Runtimeによる`AgentRuntime`契約の再検証
4. QwenCloud Model Runtimeによる`ModelRuntime`契約の確定
5. 上位アプリ向け統合catalog
6. 明示的なrouting policy
7. 必要性が確認された場合だけone-shot Agent Responses API

Provider間のfallback判断は、引き続き上位アプリの責務とする。

## 23. 公式資料

- [Muse Code SDK](https://github.com/meta-models/muse-code-sdk)
- [Muse Code Developer Docs](https://meta-models.github.io/muse-code-sdk/)
- [Muse SDK Quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/)
- [MSP concepts](https://meta-models.github.io/muse-code-sdk/guides/msp-concepts/)
- [Muse sessions and turns](https://meta-models.github.io/muse-code-sdk/guides/msp-concepts/sessions-and-turns/)
- [Muse stream a turn's answer](https://meta-models.github.io/muse-code-sdk/cookbook/stream-a-turns-answer/)
- [Muse model/list](https://meta-models.github.io/muse-code-sdk/generated/msp/methods/model-list/)
- [Muse session/start](https://meta-models.github.io/muse-code-sdk/generated/msp/methods/session-start/)
- [Muse view/unsubscribe](https://meta-models.github.io/muse-code-sdk/generated/msp/methods/view-unsubscribe/)

SDK/MSPはDeveloper Previewである。Phase 0と各実装バッチ開始時に公式version、schema fingerprint、公開型を再確認する。
