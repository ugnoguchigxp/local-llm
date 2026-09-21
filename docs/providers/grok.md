# Grok Build Agent Runtime 運用ガイド

## 状態

Grok Buildの公式ACPを使うGateway実装とmock hostテストは利用可能です。CLI `1.0.13`ではACP version 1、認証method `grok.com`、`grok-4.6`と`grok-4.5`のstructured model discoveryを実機確認しています。

Runtimeは初期状態で無効です。専用profileへのログインと、期限付きbilling証跡が揃うまでsessionやturnを開始しません。`XAI_API_KEY`を含む親processのcloud API keyはGrok childへ継承しません。

## 1. 専用profileとCLI

通常のuser homeと分離したdirectoryを0700で用意します。この端末には次の場所へ公式CLIを導入済みです。

```bash
export LOCAL_LLM_GROK_HOME="$HOME/.local/share/local-llm/provider-profiles/grok-home"
export LOCAL_LLM_GROK_BINARY="$LOCAL_LLM_GROK_HOME/.grok/bin/grok"
chmod 700 "$LOCAL_LLM_GROK_HOME"
"$LOCAL_LLM_GROK_BINARY" --version
```

`$LOCAL_LLM_GROK_HOME/.grok/requirements.toml`にはAPI key認証の禁止とsandboxを指定します。

```toml
[grok_com_config]
disable_api_key_auth = true

[sandbox]
profile = "workspace"
```

Gatewayは各session開始前に`grok inspect --json`を実行し、API key認証が無効であること、追加のproject instructions、permission rule、hook、skill、plugin、marketplace、MCP、LSP、custom agentが読み込まれていないことを確認します。許可されていない設定が見つかるとfail closedします。

CLI path、profile root、billing証跡には絶対pathを指定します。Gatewayは親processの`PATH`で見つかった同名commandを信用せず、起動するbinaryを固定します。child processへは固定`PATH`とlocaleだけを渡し、API key、custom CA、custom temporary directoryは継承しません。

このprofile内のrequirementsは誤設定防止であり、root所有のsystem policyほど強い改ざん耐性はありません。複数userが操作するhostでは、公式手順に従ってroot所有の`/etc/grok/requirements.toml`にも同じ制約を置き、`[ui] disable_bypass_permissions_mode = true`を設定してください。Gatewayはsystem policyを自動変更しません。

## 2. Subscription login

`XAI_API_KEY`を設定せず、専用profileで公式loginを実行します。次の操作はbrowserまたはdevice codeでの本人操作を伴います。

```bash
env -i \
  HOME="$LOCAL_LLM_GROK_HOME" \
  GROK_HOME="$LOCAL_LLM_GROK_HOME/.grok" \
  PATH="/usr/bin:/bin:/usr/local/bin" \
  "$LOCAL_LLM_GROK_BINARY" login --device-auth
```

credential本文、email、browser cookieをbilling証跡やGitへ保存しないでください。

## 3. Billing証跡

Grokのsubscription allowanceを超えた後、Extra Usage CreditsやAuto Top Upが追加支出経路になる可能性があります。GrokのUsage/Billing画面で次を人手確認します。

- 対象accountとplanが意図したもの
- Extra Usage Creditsの残高がzero
- Auto Top Upがdisabled
- Buildの利用がsubscription側に記録される

結果を次のschemaで保存し、0600にします。有効期限は週次reset、plan変更、login変更、CLI更新、課金設定変更より前に設定してください。

```json
{
  "schema_version": 1,
  "runtime": "grok",
  "billing_mode": "subscription",
  "billing_assurance": "operator_attested",
  "profile_root": "/absolute/path/to/grok-home",
  "auth_method": "grok.com",
  "account_fingerprint": "unavailable",
  "plan": "operator-confirmed-plan-name",
  "extra_usage_credits": "zero",
  "auto_top_up": "disabled",
  "grok_version": "1.0.13",
  "acp_protocol_version": 1,
  "model_ids": ["grok-4.6"],
  "sandbox_profile": "workspace",
  "verified_at": "2026-09-06T00:00:00+09:00",
  "expires_at": "2026-09-13T00:00:00+09:00"
}
```

schema version 1が受理するassuranceは`operator_attested`だけです。これはProviderのmachine-readableな課金元証明ではありません。文字列を`provider_verified`へ変えてもGatewayは受理しません。追加課金を技術的に絶対禁止する必要がある場合、Provider側にhard spending controlまたはsubscription-onlyのmachine-readable証明が用意されるまで有効化しないでください。

## 4. Runtime設定

```bash
export LOCAL_LLM_GROK_ENABLED=true
export LOCAL_LLM_GROK_BINARY="$LOCAL_LLM_GROK_HOME/.grok/bin/grok"
export LOCAL_LLM_GROK_BILLING_EVIDENCE_FILE="$HOME/.local/share/local-llm/grok-billing-evidence.json"
export LOCAL_LLM_GROK_ALLOWED_MODELS=grok-4.6
export LOCAL_LLM_GROK_EXPECTED_VERSION=1.0.13
export LOCAL_LLM_GROK_ACP_PROTOCOL_VERSION=1
export LOCAL_LLM_GROK_SANDBOX=workspace
export LOCAL_LLM_GROK_ALLOW_WEB_SEARCH=false
export LOCAL_LLM_GROK_ALLOW_PROJECT_EXTENSIONS=false
```

設定確認とACP handshakeはモデルturnを実行しません。

```bash
python3 scripts/check_grok_runtime.py
python3 scripts/check_grok_runtime.py --preflight
```

## 5. 実装上の安全境界

- gateway sessionごとに1つのGrok ACP processを起動する
- cwdはGatewayが作るGrok専用isolated workspace
- `--no-auto-update --sandbox workspace --permission-mode default`
- subagent、cross-session memory、plan mode、web searchをverified releaseでは無効化
- 先頭がGrok slash commandまたは`!` shell promptの入力をGatewayで拒否
- ACP clientのfilesystem/terminal capabilityをadvertiseしない
- `allow_once`と`deny`だけをHTTP APIへ公開し、always allowは選択しない
- 承認timeout、cancel、release時は保留中のpermissionをcancelledで解決する
- model、mode、session configの変更を検出したらACP processを終了し、sessionを`recovery_required`へ移す
- prompt timeoutでもcancel後にACP processを終了し、明示的resumeを要求する
- workspace設定をturn開始時と`allow_once`決定直前に再検査する
- session/turnを別Runtimeへ自動fallbackしない

Grokの`strict` sandboxは、このMacでは`/var/run/docker.sock`がsymlinkであるため起動前検査に失敗しました。公式の`workspace` profileはmacOS Seatbeltで適用され、対象workspace、Grok profile、OS一時directoryだけを書き込み可能として報告されます。ネットワークはGrok subscription通信のため許可されます。

## 6. Agent API

既存の共通endpointを使います。

```text
GET  /v1/agents/runtimes
POST /v1/agents/runtimes/grok/preflight
GET  /v1/agents/models?runtime=grok
POST /v1/agents/sessions
POST /v1/agents/sessions/{session_id}/turns
GET  /v1/agents/sessions/{session_id}/events
POST /v1/agents/sessions/{session_id}/approvals/{approval_id}/decision
POST /v1/agents/sessions/{session_id}/turns/{turn_id}/cancel
POST /v1/agents/sessions/{session_id}/release
POST /v1/agents/sessions/{session_id}/resume
```

session作成時に`runtime: "grok"`と、catalogが返す`grok/<native-model-id>`を指定します。

## 7. Live smoke

Live smokeはsubscription allowanceを消費するため、通常testでは実行しません。次の2変数を明示した場合だけ実行できます。

```bash
RUN_LIVE_GROK_TESTS=true \
ACK_GROK_SUBSCRIPTION_USAGE=true \
python3 scripts/smoke_grok_agent.py
```

file editの承認を確認する場合は、編集を求めるpromptと判断を明示します。

```bash
RUN_LIVE_GROK_TESTS=true \
ACK_GROK_SUBSCRIPTION_USAGE=true \
python3 scripts/smoke_grok_agent.py \
  --prompt "Create smoke.txt in the current workspace" \
  --approval deny
```

## 8. 停止と復旧

緊急停止は`LOCAL_LLM_GROK_ENABLED=false`に戻してAPIを再起動します。Grok障害でMuseやローカルModel APIを停止しません。

API再起動後、読み込まれていたGrok sessionは`recovery_required`になります。自動でpromptを再送せず、明示的resumeでnative session ID、workspace、model、provider、ACP fingerprintを再検証します。

## 公式資料

- [Grok Build CLI](https://docs.x.ai/build/cli)
- [Headless and ACP](https://docs.x.ai/build/cli/headless-scripting)
- [Authentication](https://docs.x.ai/build/cli/authentication)
- [Permissions](https://docs.x.ai/build/features/permissions)
- [Sandbox](https://docs.x.ai/build/features/sandbox)
