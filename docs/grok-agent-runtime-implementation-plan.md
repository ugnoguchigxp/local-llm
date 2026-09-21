# Grok Build Agent Runtime 実装計画

## 実装状況（2026-09-06）

Phase 0の実機調査、ACP transport、Runtime adapter、共通Agent Service登録、安全性・billing hardening、mock test、運用文書まで実装済み。公式CLI `1.0.13`のversion、`inspect --json`、ACP version 1のinitialize、`grok.com` auth method、model discovery、session resume/load/close capabilityをモデルturnなしで確認した。

Gatewayは`--no-plan`を含む固定起動引数を使用し、Grok slash commandと`!` shell promptを拒否する。実行中にmodel、mode、configの変更通知を受けた場合は即時cancelしてACP processを終了し、sessionを`recovery_required`へ移す。permission payloadに十分かつ省略・伏字のないtool inputがない場合はdenyだけを公開する。web searchはverified releaseでは設定で有効化できない。

実行binary、専用profile、billing証跡は絶対pathに限定する。workspaceのresolved configurationはsession作成時だけでなく、各turnと`allow_once`承認の直前にも再検査する。ACP transportはJSON-RPC 2.0 envelopeを厳格検証し、内部permission option IDを公開eventへ出さない。

残る運用作業は、利用者本人による専用profileへのsubscription loginと、期限付きbilling証跡の作成、その後の明示的opt-inによるlive smokeである。live smokeはsubscription allowanceを消費するため通常testでは実行しない。

作成日: 2026-09-06
状態: Gateway実装済み / 実アカウントでのbilling証跡とlive smokeは未実施
上位文書: [local-llm Runtime Gateway コンセプト](runtime-gateway-concept.md)

## 1. 目的

Grok Buildを、`local-llm`の2つ目の外部Agent Runtimeとして追加する。

公開済みの公式インターフェースであるAgent Client Protocol（ACP）を使用し、既存の`/v1/agents/*`へGrokのsession、turn、event、approval、cancel、resumeを接続する。Muse Runtimeと既存のローカルModel APIは変更せず、Grokが未導入・未ログイン・利用上限到達・異常終了のいずれでも他Runtimeを利用できる状態を維持する。

最初のリリースはGrok Buildのsubscriptionログインだけを対象とする。`XAI_API_KEY`によるAPI creditsまたは請求課金へ暗黙に切り替えず、課金経路を必要な水準で確認できない場合はturnを開始しない。

## 2. 結論

Grok対応は、次の2つを別Runtimeとして扱う。

| 対象 | Runtime種別 | 接続先 | 優先度 |
| --- | --- | --- | --- |
| Grok Build | Agent Runtime | `grok agent stdio`の公式ACP | 今回の実装対象 |
| xAI Inference API | Model Runtime | `/v1/responses` / `/v1/chat/completions` | 後続の任意機能 |
| Grok Bot | Agent Runtime候補 | 公開された公式APIが存在する場合のみ | 対象外 |

Grok Buildは、headless実行、session、local tool実行、permission、streaming updateを持つcoding agentである。これはMuseと同じAgent APIへ接続する。

xAI Inference APIはserver-side toolsを持つが、ローカルworkspaceの操作、permission中継、Agent Loop全体を提供するsession hostではない。将来追加する場合はModel API側へ接続し、Grok Build Runtimeへ混ぜない。

Grok Botはpersistent cloud computerを持つ別製品である。desktop appのUI操作、cookie取得、非公開通信の解析では統合しない。公式に外部client向けAPIが公開された時点で別計画を作る。

## 3. 公式仕様から確定できる前提

2026-09-06時点で、公式資料から次を確認できる。

- Grok Buildはinteractive TUI、headless script、ACP clientから利用できる。
- `grok agent stdio`はJSON-RPC over stdioのACP agentとして動作する。
- ACPでは`initialize`、`authenticate`、`session/new`、`session/prompt`、`session/update`、`session/cancel`が定義されている。
- Grok CLIはbrowser loginとdevice authenticationを提供する。
- 実機ACPは認証methodとして`grok.com`をadvertiseする。専用profileのbrowser/device loginをこのmethodで利用する。
- headless sessionはID指定、resume、continueを持つ。
- permissionの既定はAskであり、Always-approveは明示的な危険なmodeである。
- CLIは`--no-auto-update`を持ち、自動更新を停止できる。
- Grokのpaid planは週次usage poolを持ち、Usage画面ではAPI、Build、Chat等の内訳が表示される。
- Extra Usage CreditsとAuto Top Upはsubscription allowance超過後の追加課金経路になり得る。
- xAI Inference APIのAPI key経路はprepaid creditsまたはmonthly invoicingで課金される。

したがって、Grok Buildの`grok.com` subscription loginとxAI API keyを同じcredentialとして扱ってはならない。

## 4. 現在のプロジェクトとの適合

### 4.1 実装済み基盤

現在の`local-llm`には次が実装済みである。

- `AgentRuntime` Protocol
- `AgentRuntimeRegistry`
- Runtime/model catalog
- gateway session IDとturn ID
- SQLite state
- cursor付きSSE event stream
- approval decisionとuser input API
- cancel、resume、release
- isolated workspace
- Runtime単位のhealthとfail-closed設定
- Muse Runtimeによる最初のadapter実装

### 4.2 作業開始時のbaseline

2026-09-06時点のlocal環境は次の状態である。

- Python `3.14.7`
- Node.js `v24.11.1`
- 作業開始時点では`grok` binary未導入。その後、専用profileへ公式CLI `1.0.13`を導入した
- Python testは130件成功
- Muse TypeScript bridge testは12件成功
- `main`は`origin/main`と同期済み

実装開始時には次を記録した。

```bash
git status --short --branch
./.venv/bin/python --version
./.venv/bin/python -m pytest -q tests
pnpm --dir bridges/muse run test
command -v grok
grok version
grok --help
grok agent stdio --help
```

公式CLIの導入後も既存test結果を比較する。CLIが未導入の環境ではGrok Runtimeだけを`unavailable`とし、test suite全体を失敗させない。

### 4.3 実装時の実機確認結果

専用`HOME` / `GROK_HOME`で、モデルturnを実行せずに次を確認した。

- 公式CLI: `grok 1.0.13`
- transport: 改行区切りJSON-RPC 2.0 over stdio
- ACP protocol: version 1
- auth method: `grok.com`
- session capability: list、resume、close
- model discovery: `grok-4.6`と`grok-4.5`、各500,000 context tokens
- `--no-auto-update --permission-mode default --no-subagents --no-memory --disable-web-search`をACP modeで利用可能
- `grok inspect --json`で設定source、hook、skill、plugin、MCP、permission ruleを検査可能
- `requirements.toml`の`disable_api_key_auth = true`が`apiKeyAuthDisabled: true`として反映される
- `workspace` sandboxがmacOS Seatbeltでenforcedとして記録される

`strict` sandboxは`/var/run/docker.sock`がsymlinkであるため、このMacでは起動前検査に失敗した。このため初回releaseは公式`workspace` profileを固定し、ACP permission中継と設定検査を重ねる。専用profileのlogin、subscription画面の確認、billing evidence作成、live turnはoperator作業として未実施である。

### 4.4 2つ目のProviderで見直す契約

Grok Build ACPはこの契約へ概ね適合する。ただし、2つ目のProviderを追加することで次の共通契約を見直す必要がある。

- Providerごとのprotocol名とversionをstatusへ表現する方法
- billingの確認強度を`subscription`という1値だけでなく表現する方法
- Providerが返すapproval optionを`allow_once` / `deny`へ安全に写像する方法
- long-runningな`session/prompt`を非同期turnとして扱う方法
- Providerがadvertiseしないcapabilityをcatalogへ反映する方法

Grok固有の都合をMuseへ押し込まず、共通化できた項目だけを後方互換な追加fieldとしてAgent Runtime coreへ反映する。

## 5. スコープ

### 5.1 最初のリリースに含める

- Grok Build CLIの存在・version確認
- dedicated `GROK_HOME`
- `grok.com` subscription loginの検証
- secretを継承しないchild process環境
- Python製ACP client
- ACP initializeとcapability negotiation
- ACP authenticate
- session作成と再開
- text turn
- streaming `session/update`
- tool lifecycle event
- permission requestと`allow_once` / `deny`
- turn cancel
- session release
- process supervision
- model discoveryとallowlist
- subscription-only gate
- isolated workspace
- `workspace` sandboxと安全なCLI option
- mock ACP test
- opt-in live smoke test
- Provider運用ドキュメント

### 5.2 最初のリリースに含めない

- `XAI_API_KEY`の利用
- xAI Inference APIのModel Runtime
- `/v1/chat/completions`や`/v1/responses`からのGrok Build利用
- Grok Bot連携
- browser UI automation
- cookie、session token、private endpointの抽出
- Always-approve、`--yolo`
- Auto permission mode
- Grok subagent
- cross-session memory
- Grok plugin、hook、Marketplaceの自動導入
- workspaceで宣言されたMCP serverの自動許可
- web searchの既定有効化
- imageやfile attachment
- Provider間の自動fallback
- quota残量の推測
- 複数Uvicorn worker

## 6. 構成

```text
FastAPI / Python
      │
      ▼
/v1/agents/*
      │
      ▼
AgentService
      │
      ▼
GrokRuntime
      │
      ▼
GrokAcpClient / Python
      │ JSON-RPC 2.0 over stdio
      ▼
grok --no-auto-update agent stdio
      │ grok.com subscription login
      ▼
Grok Build
```

MuseのようなNode.js bridgeは追加しない。ACPは公開schemaを持つJSON-RPC protocolであり、Grok公式資料にもstdio client例がある。Pythonでtransportとschema validationを実装し、FastAPIと同じprocess treeで監視する。

将来、複数ProviderがACPを採用した場合に限り、`agent_runtime/acp/`へ共通transportを抽出する。Grok実装前に汎用ACP frameworkを作らない。

## 7. Process model

最初はgateway sessionごとに1つのGrok ACP processを起動する。

理由:

- sessionごとのcwdとworkspace境界をprocess単位で固定できる。
- process crashの影響を1 sessionへ限定できる。
- permission requestとactive turnの対応が単純になる。
- project config、MCP、pluginの混入検査をsession単位で行える。
- release時にprocessと一時resourceを確実に破棄できる。

欠点は起動costとmemory使用量である。Phase 0で測定し、既定の最大同時session数を2とする。1 processで複数sessionを安全に扱えることと、分離を維持できることが実測できた場合だけ、後続でprocess共有を検討する。

## 8. Python ACP client

`GrokAcpClient`は次だけを担当する。

- child processの起動・終了
- stdinへのJSON-RPC request/notification送信
- stdoutのNDJSON framing
- request IDとpending futureの対応
- AgentからClientへ来るrequestのdispatch
- `session/update`のdispatch
- timeoutとcancel
- stderrの長さ制限とredaction
- protocol violationの検出
- capability snapshotの保持

担当しないもの:

- HTTP route
- gateway session state
- workspaceの採用判断
- approvalの最終判断
- billing policyの最終判断
- Runtime間routing
- Provider eventのgateway eventへの意味変換

transportは以下を必須とする。

- 1 messageあたりの最大byte数
- UTF-8とJSON objectの検証
- `jsonrpc: "2.0"`の検証
- request/response/notificationの排他的なshape検証
- duplicate ID、unknown response ID、missing methodの拒否
- stdoutへ非protocol文字列が出た場合のfail closed
- pending request数の上限
- write lockによるframe混線防止
- process終了時の全pending future失敗
- stderrをstdout protocolと混ぜない

## 9. ACP handshakeとcapability

起動後は次の順で処理する。

1. `initialize`
2. protocol versionとagent capabilitiesを保存
3. advertised authentication methodsを検査
4. `grok.com`だけを選択して`authenticate`
5. model discovery手段を確定
6. `session/new`

期待するcapabilityをhardcodeせず、initialize/session responseから取得する。

最初の必須capability:

- new session
- prompt
- session update
- permission request
- cancel

ACP一般ではresume、close、session mode、elicitationはoptionalである。今回pinしたCLIではGatewayのrelease/resume契約に必要なresumeとcloseを必須とし、advertiseされなければ起動しない。user input/elicitationは初回releaseで公開しない。

protocol versionは設定された許容範囲と一致する必要がある。未知のversionへbest effortで接続しない。Grok CLIのversion、自動更新停止、ACP protocol version、capability snapshotをbilling evidenceとは別のcompatibility evidenceとして記録する。

## 10. 認証と課金経路

### 10.1 subscription-onlyの原則

最初のGrok Runtimeは専用profileの`grok.com` loginだけを使う。

- 専用の`GROK_HOME`で`grok login --device-auth`またはbrowser loginを行う。
- child processへ`XAI_API_KEY`を渡さない。
- `OPENAI_API_KEY`、Anthropic系key、cloud provider key等も渡さない。
- ACPのauth methodに`grok.com`がadvertiseされない場合は起動しない。
- `xai.api_key`しか利用できない場合は`runtime_billing_unverified`とする。
- subscription allowance超過後にAPI keyへfallbackしない。
- Grok Runtime失敗時にMuseやlocal modelへ自動fallbackしない。

### 10.2 残る課題

Grokの公式FAQでは、週次subscription allowanceの後にExtra Usage CreditsまたはAuto Top Upを利用できる。一方、ACP handshakeだけで「今回のturnが週次allowanceだけを消費する」ことを証明できるとは限らない。

このためbilling確認強度を次のように分ける。

| assurance | 意味 | turn実行 |
| --- | --- | --- |
| `provider_verified` | Providerのmachine-readable情報でsubscription-onlyを確認 | 許可 |
| `operator_attested` | 専用account、Extra Creditsなし、Auto Top Up無効を期限付き証跡で確認 | experimentalで許可 |
| `unverified` | 課金経路を確認できない | 拒否 |

`provider_verified`は将来のassurance levelである。ローカルJSONだけではProvider検証を証明できないため、billing evidence schema version 1は`operator_attested`だけを受理する。`operator_attested`には残余riskがあることをstatusと運用文書に明記する。絶対に追加課金を発生させられないことが要件なら、Providerがmachine-readableなbilling sourceまたはhard spending controlを提供するまでRuntimeを有効化しない。

### 10.3 Billing evidence

証跡fileは0600のregular fileとし、symlinkを拒否する。最低限、次を持つ。

```json
{
  "schema_version": 1,
  "runtime": "grok",
  "billing_mode": "subscription",
  "billing_assurance": "operator_attested",
  "profile_root": "/absolute/path/to/dedicated-grok-home",
  "auth_method": "grok.com",
  "account_fingerprint": "sha256:...",
  "plan": "operator-confirmed-plan-name",
  "extra_usage_credits": "zero",
  "auto_top_up": "disabled",
  "grok_version": "...",
  "acp_protocol_version": 1,
  "model_ids": ["..."],
  "verified_at": "2026-09-06T00:00:00+09:00",
  "expires_at": "2026-09-13T00:00:00+09:00"
}
```

証跡はprofile、CLI version、protocol version、allowlistと一致し、期限内でなければならない。週次reset境界、plan変更、login変更、CLI更新、課金設定変更のいずれかがあれば再作成する。

Runtimeはcredential本文、account email、tokenを保存・返却しない。`account_fingerprint`はProviderが安全に返す不透明IDをhash化できる場合だけ記録する。取得できない場合は空の代替値を捏造せず、assuranceを下げる。

## 11. Dedicated profileとchild environment

Grok用profileを共有のuser homeから分離する。

```text
~/.local/share/local-llm/provider-profiles/
└── grok-home/                  # HOME、0700
    └── .grok/                  # GROK_HOME
        ├── config.toml
        ├── requirements.toml
        └── provider-managed credential/session files
```

profile rootは0700とし、所有者、regular directory、symlink不使用を検査する。child environmentはallowlist方式で構築する。

含める候補:

- `HOME`: dedicated home
- `GROK_HOME`: dedicated Grok profile
- `PATH`:最小の固定PATH
- `LANG` / `LC_ALL`

含めない:

- `XAI_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `AWS_*`、`GOOGLE_*`、`AZURE_*`
- shell startup設定
- 親processの`TMPDIR`、custom CA / certificate path
- 親processの任意環境変数
- unrelated MCP credential

Grok BuildはClaude、Cursor、Agents系設定との互換機能を持つため、dedicated `HOME`だけでなく`grok inspect --json`のresolved sourceも検査する。許可されていないuser config、plugin、hook、MCP、custom model、inline headerを検出した場合はsessionを開始しない。

## 12. Workspaceとtool policy

最初のリリースは既存APIと同じisolated workspaceだけを許可する。

```text
~/.local/share/local-llm/agent-workspaces/
└── grok/
    └── <gateway-session-id>/
```

Grok processにはcanonicalize済みworkspaceを`cwd`として渡す。workspace外へのfilesystem accessはProviderのsandboxとOS境界の両方で制限する。

安全な既定値:

- sandboxは`workspace`（このMacでは`strict`が`/var/run/docker.sock`のsymlink検査で起動不能）
- permission modeはAsk
- `--always-approve` / `--yolo`は禁止
- subagentは無効
- cross-session memoryは無効
- auto updateは無効
- web searchは無効
- 追加MCP、plugin、hookは無効
- client-side filesystem / terminal ACP capabilityはadvertiseしない

Grokが内部toolでworkspaceを操作する構成を優先する。ACP client自身が`fs/write_text_file`や`terminal/create`を提供すると、gateway内に2つ目のtool execution engineが生じるため、初回は提供しない。Grokがそれらを必須とする場合はPhase 0で停止し、workspace policyとterminal isolationを別設計する。

## 13. Model catalog

公開IDは次とする。

```text
grok/<native-model-id>
```

Runtime IDは`grok`、Provider IDはACP認証methodと同じ`grok.com`とする。具体的なnative model IDはACPのstructured discoveryと設定allowlistの両方で検証する。

model discoveryの優先順位:

1. ACP initialize/session responseのstructured config option
2. Grok CLIの公式structured output
3. 明示的allowlistを実sessionで検証

human-readableな`grok models`出力のscreen scrapingは行わない。structured discoveryがない場合は、設定されたallowlistの各modelで副作用のないsession作成が可能かを確認し、確認できないmodelを公開しない。

catalog capability例:

```json
{
  "id": "grok/<native-model-id>",
  "runtime": "grok",
  "provider_id": "grok.com",
  "capabilities": {
    "sessions": true,
    "streaming": true,
    "provider_managed_tools": true,
    "approvals": true,
    "resume": true,
    "user_input": false,
    "attachments": false
  }
}
```

capabilityはPhase 0で得たactual advertisementに基づく。特定versionの印象からtrueを固定しない。

## 14. Sessionとturnの対応

### 14.1 Session開始

`AgentRuntime.start_session`を次へ写像する。

1. isolated workspace作成
2. dedicated profileとbilling evidence検査
3. `grok inspect --json`でresolved config検査
4. ACP process起動
5. initialize / authenticate
6. `session/new`
7. native session IDをSQLiteへ保存
8. Gateway session responseを返し、以後のturn eventをcursor付きで配信

gateway session IDだけをcallerへ返し、ACP session IDやcredential pathは内部metadataとする。

### 14.2 Turn開始

ACP `session/prompt`はturnが終わるまでresponseを返さない。そのため`start_turn`はbackground taskを作り、HTTPへはgateway turn IDと`202 Accepted`を返す。

background taskは次を行う。

- `session/prompt` requestを送る。
- `session/update`を受けてgateway eventへ変換する。
- permission requestを保留してAPI callerへ中継する。
- prompt responseのstop reasonをterminal eventへ変換する。
- process exit、timeout、protocol violationを`turn.failed`へ変換する。

1 sessionにつきactive turnは1つに制限する。Grok側にqueueがあってもgatewayで暗黙queueを作らない。

### 14.3 Cancel

cancel APIはACP `session/cancel` notificationへ写像する。

- notification送信だけで完了扱いにしない。
- pending permission requestにはACPのcancelled outcomeを返す。
- original `session/prompt`がcancelled stop reasonで完了するまで待つ。
- timeout時はprocessを終了し、sessionをresume requiredへ遷移させる。
- cancel後に遅れて届くupdateを別turnへ誤帰属させない。

### 14.4 Resume

優先順位:

1. ACP `session/resume`がadvertiseされる場合は使用
2. ACP `session/load`だけが利用可能なら使用
3. Grok固有の公式session resume手段をadapter内で使用

API再起動後は自動でturnを再送しない。callerの明示的resume時に新しいACP processを起動し、native session、workspace、model、protocol versionを再検証する。

### 14.5 Release

ACP `session/close`がadvertiseされる場合は使用し、graceful timeout後にprocessを終了する。未対応の場合はactive turnをcancelし、pending requestを解決してからprocessを終了する。session history fileの削除はreleaseの意味に含めない。

## 15. Approval mapping

ACP Agentからの`session/request_permission`を`approval.requested`へ変換する。

gateway eventは最低限次を保持する。

- gateway approval ID
- turn ID
- tool call ID
- operation title
- resourceまたはcommandの安全に表示できる要約
- callerが選べる`allow_once` / `deny`の安全な選択肢
- timeout

外部APIは当面`allow_once`と`deny`だけを受ける。

- `allow_once`はACP optionに明示的なone-time allow相当がある場合だけ選択する。
- `deny`はreject/deny once相当を選択する。
- `allow_once`をalways allowへ写像しない。
- exactな安全写像がない場合はcancelledへ倒し、`runtime_protocol_mismatch`を記録する。
- unknown optionをapprovalとして扱わない。
- timeoutはcancelledまたはdenyへ倒す。

Grok側のpermission modeをAlways-approveへ変えてapproval中継を回避してはならない。

## 16. Event mapping

ACP `session/update`を既存の標準eventへ変換する。

| ACP update | Gateway event |
| --- | --- |
| agent message chunk | `message.delta` |
| tool call開始 | `tool.started` |
| tool call update | `tool.updated` |
| tool call terminal | `tool.completed` |
| thought chunk | `reasoning.delta` |
| plan | `plan.updated` |
| permission request | `approval.requested` |
| permission response | `approval.resolved` |
| prompt開始 | `turn.started` |
| prompt success | `turn.completed` |
| cancelled stop reason | `turn.cancelled` |
| prompt/protocol error | `turn.failed` |

thoughtは`reasoning.delta`、planは`plan.updated`として通常messageと分離する。model、mode、config optionの変更は安全境界の変化として`session.invariant_changed`に写像し、processを終了する。available commandsはGatewayから実行できないため公開しない。

unknown updateはpayloadをそのまま公開せず、discriminatorだけをsize制限した`provider.event`として記録する。credential、file内容、full commandをmetadataへ残さない。

Grok adapterはsession内の単調増加cursorを発行し、Gatewayはsession/runtimeに束縛した署名付きpublic cursorへ変換する。process再起動時は永続化した最終cursorから再開し、Serviceのevent brokerで重複を除外する。

## 17. Error mapping

主要な写像:

| Grok / ACP状態 | Gateway code |
| --- | --- |
| binaryなし | `runtime_unavailable` |
| `grok.com` loginなし | `runtime_auth_required` |
| API keyしか使えない | `runtime_billing_unverified` |
| evidenceなし・期限切れ | `runtime_billing_unverified` |
| weekly allowanceまたはusage limitを示す既知message | `provider_rate_limited` |
| rate limit | `provider_rate_limited` |
| ACP version不一致 | `runtime_protocol_mismatch` |
| malformed JSON-RPC | `runtime_protocol_mismatch` |
| process異常終了 | `provider_host_exited` |
| permission option不整合 | `runtime_protocol_mismatch` |
| resume非対応 | `unsupported_capability` |
| workspace外access | `workspace_not_allowed` |

JSON-RPC error objectは整数codeと非空messageを必須とし、shape不整合はprotocol mismatchにする。認証、usage limit、cancelは既知のcodeまたはmessageだけを分類し、それ以外はbounded/redact済みのgeneric provider errorとする。subscription exhaustion専用のstructured codeは実機採取後に追加する。

API responseへstderr、credential path、native token、full commandを返さない。

## 18. Runtime statusの改善

2つ目のAgent Runtime追加時に、`RuntimeStatus`へ後方互換なoptional fieldを追加する。

```json
{
  "id": "grok",
  "status": "disabled",
  "billing_mode": "unknown",
  "billing_assurance": "unverified",
  "auth": "unknown",
  "protocol": {
    "name": "acp",
    "version": null
  },
  "host_version": null,
  "active_sessions": 0,
  "active_turns": 0,
  "detail": null
}
```

既存の`protocol_fingerprint`は維持する。MuseはMSP fingerprint、Grokは互換性snapshotのhashを返せる。新fieldを読まない既存clientを壊さない。

statusは少なくとも次を区別する。

- `disabled`
- `unavailable`
- `auth_required`
- `billing_unverified`
- `starting`
- `ready`
- `degraded`

FastAPI全体の`/health`はGrokの失敗でunhealthyにしない。`/status`と`/v1/agents/runtimes`でGrok固有状態を返す。

## 19. 設定案

```dotenv
# Grok Build Agent Runtime (optional, disabled and fail-closed by default)
LOCAL_LLM_GROK_ENABLED=false
LOCAL_LLM_GROK_BINARY=/absolute/path/to/grok
LOCAL_LLM_GROK_HOME=
LOCAL_LLM_GROK_BILLING_EVIDENCE_FILE=
LOCAL_LLM_GROK_ALLOWED_MODELS=
LOCAL_LLM_GROK_EXPECTED_VERSION=
LOCAL_LLM_GROK_ACP_PROTOCOL_VERSION=
LOCAL_LLM_GROK_MAX_SESSIONS=2
LOCAL_LLM_GROK_STARTUP_TIMEOUT_MS=10000
LOCAL_LLM_GROK_REQUEST_TIMEOUT_MS=30000
LOCAL_LLM_GROK_TURN_TIMEOUT_MS=900000
LOCAL_LLM_GROK_SHUTDOWN_TIMEOUT_MS=30000
LOCAL_LLM_GROK_APPROVAL_TIMEOUT_MS=300000
LOCAL_LLM_GROK_SANDBOX=workspace
LOCAL_LLM_GROK_ALLOW_WEB_SEARCH=false
LOCAL_LLM_GROK_ALLOW_PROJECT_EXTENSIONS=false
LOCAL_LLM_GROK_DEBUG_LOG=false
```

`LOCAL_LLM_GROK_AUTH_METHOD`や`LOCAL_LLM_GROK_ALWAYS_APPROVE`は設けない。初回releaseで選択肢を増やすと、安全でない経路を設定だけで有効化できてしまうためである。

`XAI_API_KEY`はGrok Agent Runtime設定に含めない。将来のxAI Model Runtimeでは別prefix、別credential、別billing modeで定義する。

## 20. 予定ファイル

```text
agent_runtime/
├── base.py                         # optional status field
├── service.py                      # Grok registration
└── grok/
    ├── __init__.py
    ├── acp_client.py
    ├── config.py
    ├── error_mapping.py
    ├── event_mapping.py
    └── runtime.py

tests/
├── fixtures/
│   └── grok_acp/
├── test_grok_acp_client.py
├── test_grok_config.py
├── test_grok_error_mapping.py
├── test_grok_event_mapping.py
└── test_grok_runtime.py

scripts/
├── check_grok_runtime.py
└── smoke_grok_agent.py

docs/
├── grok-agent-runtime-implementation-plan.md
└── providers/
    └── grok.md
```

ACP schemaをvendoringする場合は、取得元のcommit、license、schema hashを記録する。schema全体を手書きで複製せず、実際に送受信するsubsetをruntime validationし、unknown fieldを許容しつつunknown discriminatorを安全に処理する。

## 21. 実装フェーズ

### Phase 0: Feasibility、billing、security gate

コード本体へ統合する前に専用profileで確認する。

1. 公式Grok CLIを導入し、versionを記録する。
2. dedicated `HOME` / `GROK_HOME`を0700で作る。
3. API keyなしでdevice/browser loginする。
4. `grok inspect --json`の出力とconfig sourceを確認する。
5. `grok --no-auto-update agent stdio`を起動する。
6. initialize response、protocol version、capabilities、auth methodsを保存する。
7. `grok.com` loginでauthenticateする。
8. model discovery方法を確認する。
9. isolated workspaceでsessionを作る。
10. read-only turnを完了する。
11. file editがpermission requestを発生させることを確認する。
12. allow onceとdenyを実機確認する。
13. cancel terminal状態を確認する。
14. process再起動後のresumeを確認する。
15. session closeまたは安全なreleaseを確認する。
16. secretsなしのchild environmentで動くことを確認する。
17. subscription Usage画面のBuild消費と課金設定を確認する。
18. Extra Usage Creditsがzero、Auto Top Upがdisabledであることを確認する。
19. billing evidenceとcompatibility transcriptを作る。

停止条件:

- `grok.com` loginをACPで選べない。
- `XAI_API_KEY`なしではheadless ACPが動かない。
- approvalをAlways-approveにしないと動かない。
- `workspace` sandboxまたは同等の境界を強制できない。
- workspace外accessを制限できない。
- project config、plugin、hook、MCPの混入を検査できない。
- cancel後もtoolが継続する。
- sessionとturn updateを一意に対応できない。
- subscriptionと追加課金の分離が要求水準を満たさない。

### Phase 1: ACP transport

- `GrokAcpClient`
- subprocess lifecycle
- JSON-RPC framing
- initialize/authenticate
- bidirectional request
- notification
- timeout/cancel
- size/queue limits
- protocol transcript fixture
- fake ACP process test

完了条件:

- 実Grok CLIなしで正常系と異常系をCI検証できる。
- malformed frameやhost exitでpending requestが残らない。
- Agentからのpermission requestへ非同期に応答できる。

### Phase 2: Grok Runtime adapter

- configとpreflight
- process-per-session管理
- catalog
- start/resume/release session
- background prompt turn
- event mapping
- approval mapping
- cancel
- error mapping
- capability negotiation

完了条件:

- 既存`AgentRuntime` contract testをGrok adapterでも通せる。
- Muse固有testを壊さない。
- unsupported capabilityを明示できる。

### Phase 3: RegistrationとAPI統合

- `build_agent_service()`へMuseとGrokを登録
- disabled Runtimeを安全に列挙
- `/v1/agents/runtimes`へGrok statusを表示
- `/v1/agents/models?runtime=grok`
- 既存session/turn/event/approval/cancel routeをそのまま利用
- statusのbilling assuranceとprotocol metadata

新しいGrok専用HTTP routeは作らない。Provider選択はsession作成時の`runtime: "grok"`で行う。

完了条件:

- Grok disabledでも既存local APIとMuse APIが回帰しない。
- runtime/model IDの取り違えを404またはprotocol mismatchで拒否する。
- Provider間fallbackが発生しない。

### Phase 4: Securityとsubscription gate

- dedicated profile検証
- child env allowlist
- resolved config inspection
- billing evidence schema
- evidence expiry
- CLI/protocol version pin
- secret redaction
- sandbox/permission enforcement
- approval timeout
- workspace escape test

完了条件:

- API keyを親processへ設定していてもGrok childへ継承しない。
- evidence不備、期限切れ、version差異でturnを拒否する。
- Always-approveや未知のpermission optionを許可しない。

### Phase 5: 運用とlive smoke

- `check_grok_runtime.py`
- `smoke_grok_agent.py`
- `docs/providers/grok.md`
- `.env.example`
- README
- process/resource metrics
- crash/restart/release test
- subscription exhaustion時の実error採取

live smokeは明示的opt-inとし、read-only、approval deny、approval allow once、cancel、resumeの順で行う。実課金またはsubscription usageを消費するため、通常CIでは実行しない。

### Phase 6: xAI Model Runtimeの再評価

Grok Agent Runtime安定後、別の計画で次を検討する。

- xAI `/v1/models` discovery
- xAI Responses API
- Chat Completions
- streaming
- client-side function call
- server-side web/X/code tools
- prompt cache key
- stateful responseとdata retention
- API creditとmonthly invoiceのhard limit

このPhaseを実装してもRuntime ID、credential、billing mode、catalogはGrok Buildから分ける。subscription Agent RuntimeからAPI credit Runtimeへ自動fallbackしない。

## 22. Test計画

### 22.1 Unit test

- config default disabled
- binary/profile/evidence validation
- environment allowlist
- secret exclusion
- JSON-RPC request/response correlation
- notification ordering
- Agent initiated request
- malformed/oversized frame
- duplicate/unknown ID
- timeout
- process exit
- graceful shutdown
- event mapping
- approval exact mapping
- unknown approval optionのdeny
- stop reason mapping
- error redaction
- catalog prefixとduplicate検出

### 22.2 Service / route test

- MuseとGrokの同時登録
- runtime filter
- Grok session lifecycle
- 1 session 1 active turn
- idempotency key
- SSE reconnect
- cancel race
- approval race
- API restart後resume
- release中のactive turn
- disabled/unavailable/billing unverified isolation

### 22.3 Adversarial test

- child envのdummy API key流出検査
- symlink profile/evidence/workspace
- world-readable credential/evidence
- project `.grok/config.toml`によるMCP注入
- custom modelによる外部base URL注入
- plugin/hook注入
- stdoutへのsecret混入
- stderr flood
- update flood
- permission request ID再利用
- allow onceからallow alwaysへの誤写像
- cancel直後のlate update
- resume replayのevent重複

### 22.4 Regression

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests
pnpm --dir bridges/muse run test
git diff --check
```

実Grok smokeは別commandにし、credentialがない環境ではskipではなく「未実施」を明確に表示する。

## 23. 完了条件とrelease gate

Gateway実装の完了条件はmock/contract/regression testで確認する。実アカウントを用いる次の3項目はrelease gateであり、operatorの明示操作まで未完了として扱う。

- Grok未導入でも既存APIが起動する。
- Grok disabledが既定である。
- API keyなし、`grok.com` loginだけでACP接続できる。（release gate）
- billing assuranceをstatusで確認できる。
- assuranceが`unverified`ならturnを拒否する。
- isolated workspaceが既定かつ唯一の初回選択肢である。
- Ask permissionを維持する。
- `allow_once`がpersistent approvalへ昇格しない。
- session、turn、tool、approval、terminal eventをSSEで取得できる。
- cancelがterminal状態まで追跡される。
- API再起動後に明示的resumeできる。
- process crashを他Runtimeから隔離できる。
- Provider固有IDやcredentialを外部APIへ漏らさない。
- modelはdynamic discoveryとallowlistの双方を通る。
- unknown protocol/version/capabilityをfail closedする。
- mock testを通常CIで実行できる。
- billing証跡を実アカウントの画面確認に基づいて作成する。（release gate）
- live smokeは明示的opt-inで成功する。（release gate）
- 既存Python testとMuse bridge testがすべて成功する。

## 24. 実機確認済み事項と未確定事項

モデルturnを実行せずに、次を確認済みである。

- CLI version `1.0.13`と絶対path pin
- ACP protocol version 1
- auth method ID `grok.com`
- resume / close capability
- initialize metadataによるmodel catalog
- `inspect --json`によるresolved config検査
- `workspace` sandboxの起動と報告値

実アカウントの明示的live smokeまで未確定なのは次である。

- permission optionの実payload
- tool updateの種類とterminal条件
- prompt stop reasonとcancel収束
- process再起動後のresume挙動
- subscription exhaustionのstructured error
- weekly allowanceとExtra Usage Creditsのmachine-readableな区別
- telemetryとdata retention設定

未確定値を推測で実装しない。Phase 0 transcriptはcredentialとpromptを除去してfixture化し、以後のcontract testの正本とする。

## 25. 実装順序

1. Phase 0の実機調査
2. 調査結果と停止条件のレビュー
3. ACP transportとfake host test
4. Grok Runtime adapter
5. Service登録とAPI regression
6. Security / billing hardening
7. live smoke
8. Provider運用文書
9. 全testとcode review
10. Grok Agent Runtime release判定

Phase 0を通過する前に本番Runtimeを有効化しない。特に、API key経路で動いたことをsubscription経路の成功とみなさない。

## 26. 参考となる公式資料

- [Grok Build overview](https://docs.x.ai/build/overview)
- [Grok Build Headless & Scripting / ACP](https://docs.x.ai/build/cli/headless-scripting)
- [Grok Build CLI Reference](https://docs.x.ai/build/cli/reference)
- [Grok Build Settings](https://docs.x.ai/build/settings)
- [Grok Build Permissions](https://docs.x.ai/build/features/permissions)
- [Grok Build MCP Servers](https://docs.x.ai/build/features/mcp-servers)
- [Grok Website / Apps FAQ](https://docs.x.ai/grok/faq)
- [xAI Inference API](https://docs.x.ai/developers/rest-api-reference/inference)
- [xAI Models API](https://docs.x.ai/developers/rest-api-reference/inference/models)
- [xAI Billing](https://docs.x.ai/console/billing)
- [Agent Client Protocol overview](https://agentclientprotocol.com/protocol/overview)
- [Agent Client Protocol schema](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/schema/v1/schema.json)

Grok Build、ACP、model、plan、billing仕様はいずれも更新され得る。実装時はこの計画書のmodel名やversion例ではなく、公式CLIのstructured discovery、ACP handshake、公式release notesを正本とする。
