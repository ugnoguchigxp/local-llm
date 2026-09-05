# local-llm Runtime Gateway コンセプト

作成日: 2026-09-06
状態: 採用済み / Muse Gateway実装済み（実subscription検証待ち）

Muse向けの具体的な変更順序と検証条件は、[Muse Agent Runtime 実装計画](muse-agent-runtime-implementation-plan.md)を参照する。

## 1. この文書の目的

`local-llm` を、既存のローカル推論を壊さずに、定額サブスクリプションで利用できる外部AI実行環境へ接続できるローカルGatewayへ発展させる。

最初に実装する外部Runtimeは **Muse Code** とする。将来は、同じ構造のAgent RuntimeとしてGrok系、Raw Model RuntimeとしてQwen系クラウドを追加できる形にする。

ただし、将来のProviderを想像して巨大な共通抽象を先に作ることはしない。Museで実際に確認できた契約を最初の基準とし、2つ目の実装を追加する時点で共通部分を確定する。

この文書は完成形の方向と、Muse実装を安全に始めるための境界を定める。個別メソッドの詳細、環境変数一覧、運用手順は実装計画とProvider別ドキュメントで扱う。

## 2. 一文で表すコンセプト

> `local-llm` は、ローカルモデル、外部Raw Model、外部Agent Runtimeを、課金経路と実行責務を混同せずに提供するローカルAI Runtime Gatewayである。

想定する最終構成:

| Runtime | 実行面 | 接続方針 | 実装時期 |
| --- | --- | --- | --- |
| Local Qwen / Gemma等 | Model API | 既存local daemon | 提供済み |
| Muse Code | Agent API | 公式SDK / MSP | 最初に実装 |
| Grok系Agent | Agent API | 正式提供されるAgent protocol | Muse安定後 |
| Qwen系Cloud | Model API | 正式なsubscription API | Muse安定後 |

GrokやQwenを「Museのfallback」として固定するのではなく、それぞれ独立したRuntimeとして登録する。どのRuntimeを選ぶか、quota超過後に何へ切り替えるかは、原則としてSAAA、ContextStill、NightWorkersなどの上位アプリが判断する。

## 3. 最初に確定する設計判断

1. OpenAI互換のModel APIと、状態を持つAgent APIを分離する。
2. MuseはModel APIへ擬態させず、最初はAgent APIだけで提供する。
3. Muse公式TypeScript SDKは、薄いNode.js bridgeを介してPython/FastAPIから利用する。
4. subscription利用を機械的に確認できない状態では、外部Runtimeを利用可能にしない。
5. workspace、approval、user input、cancel、resumeをAgent Runtimeの中核契約に含める。
6. 既存のlocal model IDとAPI契約は維持する。
7. GrokとQwenは完成形に含めるが、Museの初回実装には含めない。

## 4. なぜAPIを2つの実行面に分けるのか

ローカルモデルやOpenAI互換のQwen系クラウドは、基本的に入力からモデル出力を得る **Model Runtime** である。tool callを返すことはあっても、toolの実行とAgent Loopは呼び出し元が管理する。

Muse Codeや将来のGrok系Agentは、session、tool execution、approval、途中イベント、再開を持つ **Agent Runtime** である。単純なtext-in/text-outへ変換すると、重要な状態や安全機構が失われる。

したがって、最終構造を次のように分ける。

```text
                           local-llm
                               │
             ┌─────────────────┴─────────────────┐
             │                                   │
        Model API                            Agent API
  OpenAI-compatible contract           Session/event contract
             │                                   │
     ModelRuntimeRegistry                AgentRuntimeRegistry
             │                                   │
      ┌──────┴──────┐                     ┌──────┴──────┐
      │             │                     │             │
 Local Model    Qwen Cloud              Muse          Grok
  existing        future                first         future
      │             │                     │             │
 local daemon   official API          SDK / MSP    official agent
                                                    protocol only
```

この分離により、NightWorkersなどが自分でAgent Loopを持つ場合はModel APIを使い、作業全体を外部Agentへ委譲する場合はAgent APIを使える。

## 5. Runtimeの責務

### 5.1 Model Runtime

対象:

- 既存のローカルモデル
- 将来のQwen系subscription API
- 将来追加される正式なRaw Model API

責務:

- model一覧
- chat completions
- responses
- streaming
- tool callの返却
- provider固有エラーの正規化
- 認証経路と課金種別の確認

非責務:

- toolの実行
- Agent Loop
- filesystemやshellの操作
- Provider間の自動fallback

概念契約:

```python
class ModelRuntime(Protocol):
    async def list_models(self): ...
    async def chat_completions(self, request): ...
    async def responses(self, request): ...
    async def health(self): ...
    async def close(self): ...
```

既存ローカル実装を、最初からこのProtocolへ全面移行させる必要はない。Qwen系Runtimeを追加する時点で、既存daemonを薄いadapterで包み、実際に共通する契約を確定する。

### 5.2 Agent Runtime

対象:

- Muse Code
- 将来のGrok系Agent
- 将来追加される正式なsession型Agent Runtime

責務:

- 利用可能なAgent/modelの列挙
- sessionの開始と再開
- turnの開始とcancel
- event stream
- approvalの決定
- Agentからのuser input要求への回答
- workspace policy
- host processの監視
- provider固有エラーの正規化

概念契約:

```python
class AgentRuntime(Protocol):
    async def list_models(self): ...
    async def start_session(self, request): ...
    async def resume_session(self, session): ...
    async def start_turn(self, session, request): ...
    async def cancel_turn(self, session, turn_id): ...
    async def decide_approval(self, session, request): ...
    async def answer_user_input(self, session, request): ...
    async def stream_events(self, session, cursor): ...
    async def health(self): ...
    async def close(self): ...
```

Muse固有のMSP frameやGrok固有のprotocol objectは、この境界の外へ漏らさない。一方で、共通形式に変換できない情報を黙って捨てないよう、redact済みの`provider_metadata`を任意で保持できるようにする。

## 6. 共通化するものと、共通化しないもの

### 共通化する

- Runtimeの登録と有効・無効状態
- catalog metadata
- gateway session ID
- event envelope
- error envelope
- workspace policy
- approvalとuser inputのAPI表現
- timeout、cancel、idempotency
- secrets masking
- subscription-only policy
- health/statusの表現

### Providerごとに保持する

- credentialの保存場所とlogin方法
- native model IDとprovider ID
- session/turnのnative ID
- protocol handshake
- quota情報の取得可否
- native eventの種類
- host processの起動・終了方法
- provider固有のapproval semantics

共通化できない差異をbooleanの羅列で吸収し続けない。必要な場合はRuntimeごとのcapability objectと、明示的な`unsupported_capability`エラーで扱う。

## 7. CatalogとID設計

### 7.1 APIごとに利用可能な対象を分ける

`GET /v1/models`には、`/v1/chat/completions`または`/v1/responses`で実際に呼べるModel Runtimeだけを返す。

Agent Runtimeは`GET /v1/agents/models`へ返す。MuseをAgent APIでしか呼べない段階で、OpenAI互換の`/v1/models`へ混在させない。

将来、両方を一度に発見したい内部クライアント向けに`GET /v1/catalog`を追加してもよいが、OpenAI互換契約とは分離する。

### 7.2 ID

Model API:

```text
local/qwen-3.6-14b-it
local/gemma-4-e4b-it
qwencloud/<native-model-id>
```

Agent API:

```text
muse/<native-model-id>
grok/<native-agent-or-model-id>
```

既存のprefixなしlocal model IDは後方互換のaliasとして維持する。

外部model IDをソースコードへ固定しない。Museでは`model/list`に相当する公式のdiscovery結果からcatalogを構築し、allowlistに合うものだけを公開する。GrokとQwenも、公式のdiscovery手段が存在する場合は同じ方針とする。

### 7.3 Catalog metadata

```json
{
  "id": "muse/<native-model-id>",
  "runtime_type": "agent",
  "provider": "muse",
  "billing_mode": "subscription",
  "availability": "ready",
  "capabilities": {
    "sessions": true,
    "streaming": true,
    "provider_managed_tools": true,
    "approvals": true,
    "resume": true
  }
}
```

`billing_mode`は設定値だけから断定しない。実行時に検証できた場合だけ`subscription`とし、確認不能なら`unknown`としてRuntimeを利用不可にする。

## 8. Agent APIの外部契約

最初のAPI候補:

```text
GET  /v1/agents/runtimes
GET  /v1/agents/models

POST /v1/agents/sessions
GET  /v1/agents/sessions/{session_id}
POST /v1/agents/sessions/{session_id}/resume
POST /v1/agents/sessions/{session_id}/release

POST /v1/agents/sessions/{session_id}/turns
POST /v1/agents/sessions/{session_id}/turns/{turn_id}/cancel
GET  /v1/agents/sessions/{session_id}/events?after={cursor}

POST /v1/agents/sessions/{session_id}/approvals/{approval_id}/decision
POST /v1/agents/sessions/{session_id}/user-input/{request_id}/answer
```

`turns`は処理完了までHTTP接続を占有せず、原則として`202 Accepted`とgateway turn IDを返す。進捗と最終結果はcursor付きSSE event streamで取得する。

イベントの最小共通形:

```json
{
  "cursor": "opaque-cursor",
  "type": "message.delta",
  "session_id": "ags_...",
  "turn_id": "agt_...",
  "created_at": 0,
  "data": {}
}
```

標準event type:

- `session.started`
- `session.resumed`
- `turn.started`
- `message.delta`
- `message.completed`
- `tool.started`
- `tool.updated`
- `tool.completed`
- `approval.requested`
- `approval.resolved`
- `user_input.requested`
- `user_input.resolved`
- `turn.completed`
- `turn.cancelled`
- `turn.failed`
- `turn.unqueued`
- `runtime.warning`

Providerのackを処理完了とみなさない。terminal eventを受け取って初めてturnを完了状態にする。Museのqueued turnが実行前に外れた場合は`turn/unqueued`をterminalとして扱い、`turn.completed`を待ち続けない。

## 9. Museを統合するプロセス境界

Muse公式SDKはTypeScript向けであり、`local-llm`のAPI本体はPython/FastAPIである。公式SDKのsession、command idempotency、event fold、schema fingerprint対応を利用するため、PythonでMSPを独自再実装せず、薄いNode.js bridgeを置く。

```text
FastAPI / Python
      │
      │ internal NDJSON protocol
      ▼
Muse Bridge / Node.js
      │
      │ @muse-code/sdk
      ▼
local muse serve process
      │
      │ authenticated official route
      ▼
Muse Code subscription
```

bridgeの責務:

- `muse serve`の起動とhandshake
- schema fingerprintの取得と互換性判定
- model catalogの取得
- MSP command IDとgateway request IDの対応
- notificationの正規化と転送
- approval/user input要求の転送
- graceful shutdown
- host異常終了の通知
- session resume

bridgeの非責務:

- HTTP認証
- workspaceの許可判断
- Provider間routing
- PAYG fallback
- promptやresponseの永続保存

bridgeはAPI processごとに1つを基本とし、複数sessionを多重化する。複数Uvicorn workerによる同一sessionの競合を避けるため、Agent Runtime有効時は単一workerを前提とする。将来multi-process化する場合は、bridgeを独立daemonに昇格させる。

Muse v1では1 hostに1 client connectionを持たせ、その接続上で複数sessionを扱う。session単位の削除やnative `close`は前提にせず、gatewayのreleaseは`view/unsubscribe`相当で購読を解除する。Provider側のdurable historyは保持され、必要なら後からresumeする。

## 10. Subscription-only policy

「PAYGへ切り替えない」は運用上のお願いではなく、起動条件として実装する。

### 必須条件

- Muse Code用に専用のprofile homeを使用する。
- browser cookieや非公開tokenを取得しない。
- 子プロセスへ渡す環境変数をallowlist方式にする。
- PAYG用API keyを子プロセスへ継承しない。
- handshake、provider、model catalogから期待した経路を確認する。
- 許可済みprovider IDとmodel IDだけを公開する。
- 課金経路が曖昧な場合は`runtime_billing_unverified`でfail closedする。
- quota超過時は429へ正規化し、別Providerへ自動fallbackしない。

公式protocolからsubscription/PAYGの違いを機械的に識別できない場合、環境分離とcredential provenanceを検証した上で`experimental`として扱う。それでも課金経路を合理的に保証できなければ、本番利用可能にはしない。

将来のQwenやGrokについても同じ原則を適用する。Providerごとの「subscription credential」と「PAYG credential」が区別できない場合、そのProviderをsubscription Runtimeとして登録しない。

## 11. Security model

### 11.1 workspace

デフォルトはsessionごとの隔離workspaceとする。

```text
~/.local/share/local-llm/agent-workspaces/
└── muse/
    └── <gateway-session-id>/
```

外部directoryを使う場合は、callerが明示的に指定し、gateway側のallowed rootsにも含まれている必要がある。パスはcanonicalize後に判定し、symlinkで許可範囲外へ出られないようにする。

### 11.2 approval

- 初期値は最も制限の強い対話可能なapproval modeとする。
- `yolo`相当やapproval無効化をAPIの既定値にしない。
- approval要求はSSEでcallerへ返し、明示的なdecision APIで回答する。
- approval待機中はturnをrunning扱いにせず、`waiting_for_approval`を表現する。
- timeout時はallowではなくdeny/cancel側へ倒す。

### 11.3 secretsとログ

- credential、Authorization header、provider tokenを返さない。
- prompt、tool arguments、file内容は通常ログへ記録しない。
- stderrとprovider errorは長さを制限し、既知secretをmaskする。
- debug loggingは明示的opt-inとし、保存先とretentionを設定可能にする。
- metricsには件数、時間、状態、token usageだけを記録する。

## 12. Session stateと再起動

MuseなどProvider側が保持する会話履歴をGatewayへ二重保存しない。Gatewayは再接続に必要な最小metadataだけを保持する。

保存候補:

- gateway session ID
- runtime名
- provider session ID
- workspace pathとpolicy
- model ID
- 最後に確認したevent cursor
- 作成・更新時刻
- session status
- protocol fingerprint

保存先は標準ライブラリで扱えるSQLiteを第一候補とする。認証情報、prompt本文、response本文は保存しない。

API再起動後は、sessionを自動実行せず、最初の参照または明示的`resume`でProvider sessionへ再接続する。再開時にworkspace、provider、model、fingerprintを再検証する。

## 13. Concurrencyとidempotency

- 1 sessionにつき同時に1 active turnを原則とする。
- 暗黙の長いqueueを作らず、競合時は409または明示的queued状態を返す。
- session間の最大並列数を設定可能にする。
- HTTPの`Idempotency-Key`をnative command IDへ対応付ける。
- cancelのHTTP応答を完了とみなさず、terminal eventを待つ。
- host異常終了後にturnを自動再送しない。重複実行を避け、resume可能な状態としてcallerへ返す。

## 14. Error contract

共通形式:

```json
{
  "error": {
    "code": "provider_subscription_exhausted",
    "message": "Muse subscription quota is exhausted.",
    "runtime": "muse",
    "retryable": true,
    "retry_after": null,
    "details": {}
  }
}
```

主要code:

- `runtime_disabled`
- `runtime_unavailable`
- `runtime_auth_required`
- `runtime_billing_unverified`
- `provider_subscription_exhausted`
- `provider_rate_limited`
- `unsupported_capability`
- `session_not_found`
- `session_in_use`
- `turn_in_progress`
- `approval_required`
- `provider_protocol_mismatch`
- `provider_host_exited`
- `workspace_not_allowed`

Providerのquota残量やreset時刻が公式interfaceから得られない場合は、推測値を返さず`null`または`unsupported`とする。

## 15. Healthとstatus

`/health`はAPI自体の生存確認を担い、Museが未導入でも既存local APIを起動できるようにする。外部Runtimeの起動失敗でFastAPI全体を停止させない。

`/status`または`/v1/agents/runtimes`では次を返す。

```json
{
  "id": "muse",
  "runtime_type": "agent",
  "status": "disabled",
  "billing_mode": "unknown",
  "auth": "unknown",
  "protocol_fingerprint": null,
  "active_sessions": 0,
  "active_turns": 0,
  "last_error": null
}
```

状態は最低限、`disabled`、`starting`、`ready`、`degraded`、`unavailable`を区別する。

## 16. 推奨ディレクトリ構造

Muse実装時点:

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
    └── runtime.py

bridges/
└── muse/
    ├── package.json
    ├── tsconfig.json
    ├── src/
    │   ├── main.ts
    │   ├── protocol.ts
    │   └── redact.ts
    └── tests/

api/
├── routes/
│   └── agents.py
└── agent_schemas.py
```

将来:

```text
agent_runtime/
├── muse/
└── grok/

model_runtime/
├── local.py
└── qwencloud/
```

既存の`core/provider_profiles.py`はローカルモデルfamilyごとの生成profileであり、このRuntime構造とは別責務のまま維持する。

## 17. 実装ロードマップ

### Phase 0: Muse feasibility gate

コード本体を変更する前に、専用profileで次を確認する。

- `muse` binaryが起動する。
- `muse serve`とSDK handshakeが成功する。
- protocol fingerprintを取得できる。
- model catalogから利用可能なnative IDを取得できる。
- Muse Code subscription経路であることを合理的に確認できる。
- PAYG keyなしでturnを実行できる。
- isolated workspaceでtext turnを完了できる。
- approval、cancel、resumeを実機確認できる。
- host異常終了を検出できる。

subscription経路を確認できなければ、以降へ進まない。

### Phase 1: Muse bridge

- `@muse-code/sdk`をexact versionで固定
- internal NDJSON contract
- handshake、catalog、session、turn、events
- fingerprint policy
- mock hostと公式transcriptを使うtest
- secrets masking

### Phase 2: Agent Runtime coreとAPI

- `AgentRuntime`最小契約
- Muse Runtime adapter
- session state
- isolated workspace
- Agent API
- SSE cursor/resume
- approval/user input/cancel
- existing API authの適用

### Phase 3: Lifecycleと運用

- bridge supervision
- API再起動後のsession resume
- concurrency limit
- status/metrics
- quota/error mapping
- opt-in live smoke
- Muse運用ドキュメント

### Phase 4: Muse reference implementation完了

- 実クライアント1つからend-to-end利用
- 既存local model APIの回帰確認
- subscription-only監査
- workspace escape test
- failure/recovery test

ここまでが最初のリリース範囲である。

### Phase 5: 2つ目以降のRuntime

1. Grok系の正式なAgent protocolとsubscription利用条件を確認する。
2. Museとの差分を基に`AgentRuntime`契約を必要最小限だけ修正する。
3. Qwen系の正式なsubscription APIとPAYG分離を確認する。
4. Qwen実装時に`ModelRuntime`と既存local adapterを確定する。
5. 複数Runtimeを跨ぐroutingは上位アプリの要件が確認できてから導入する。

## 18. 最初のリリースに含めないもの

- Grok Runtimeの実装
- QwenCloud Runtimeの実装
- Provider間の自動fallback
- quota最適化routing
- Museを`/v1/chat/completions`へ変換する互換mode
- Agent tool callをcaller-side tool callへ逆変換する処理
- 複数Uvicorn worker対応
- 任意workspaceへの無条件アクセス
- 推測によるquota残量表示

これらは将来像から削除するのではなく、Museのreference implementationが安定するまで延期する。

## 19. Muse初回リリースの完了条件

- Muse未導入・未ログインでも既存local APIが従来通り起動する。
- 既存local model IDとOpenAI互換endpointが変わらない。
- Muse modelはAgent専用catalogからのみ公開される。
- Muse subscription経路を確認できない場合はturnを拒否する。
- PAYG credentialをMuse child processへ渡さない。
- isolated workspaceが既定となる。
- approvalとuser inputをcallerへ中継できる。
- text、tool activity、terminal状態をSSEで受け取れる。
- cancel後にterminal eventを確認できる。
- API再起動後にsessionをresumeできる。
- bridge/host異常終了を統一errorへ変換できる。
- credentialとprompt本文を通常ログへ出さない。
- mock testを通常CIで実行できる。
- live testは明示的opt-inでのみ実行される。
- 既存テストと新規テストがすべて成功する。

## 20. 未確定事項と停止条件

次の項目は実機確認で確定する。

- Muse subscriptionを示すmachine-readableな情報
- Museで実際に利用可能なprovider IDとmodel ID
- quota exhaustion時のnative error
- SDKとhostのfingerprint不一致時に安全に継続できる範囲
- 1 hostで安全に多重化できるsession数
- approval modeの正式な名前と挙動
- session logとworkspaceのretention

次の場合は実装を止めて設計を見直す。

- subscriptionとPAYGを区別できない。
- SDK経由でもsubscription利用が保証されない。
- approvalを無効にしないとheadless実行できない。
- workspace境界を強制できない。
- protocol変更を検出できない。

## 21. 参考となる公式資料

- [Muse Code SDK](https://github.com/meta-models/muse-code-sdk)
- [Muse Code Developer Docs](https://meta-models.github.io/muse-code-sdk/)
- [Muse SDK Quickstart](https://meta-models.github.io/muse-code-sdk/guides/quickstart/)
- [MSP concepts](https://meta-models.github.io/muse-code-sdk/guides/msp-concepts/)
- [Meta AI - Muse](https://ai.meta.com/llama)

Muse SDKとMSPはDeveloper Previewである。実装時はバージョンを固定し、公式schema fingerprintとrelease notesを基準に互換性を判断する。
