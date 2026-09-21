# MiniMax Token Plan Provider 実装計画

作成日: 2026-09-06
状態: 計画のみ / 未実装
上位文書: [local-llm Runtime Gateway コンセプト](runtime-gateway-concept.md)

## 1. 目的

MiniMax Token Planを、`local-llm`のsubscription専用Providerとして追加する。

初回リリースでは、同じSubscription Keyで正式に提供される次の3能力を対象とする。

- MiniMax M3によるテキスト生成
- `image-01`による画像生成
- `speech-2.8-hd` / `speech-2.8-turbo`による通常TTS

標準Open Platform API KeyによるPAYG、購入済みCreditsへの意図しない移行、他Providerへの自動fallbackは行わない。サブスクリプション経路と対象能力を必要な水準で確認できない場合は、該当Runtimeを有効にしない。

ここでHTTP APIは通信手段であり、課金方式を意味しない。本計画で利用するcredentialはToken PlanのSubscription Keyだけである。

## 2. 結論

MiniMaxは、Muse CodeやGrok Buildのようなsession型Agent Runtimeではなく、能力別のRaw Generation Runtimeとして実装する。

| 公開ID | 種別 | Token Plan | 初回実装 |
| --- | --- | --- | --- |
| `minimax/MiniMax-M3` | Model Runtime | 対象 | 含める |
| `minimax/image-01` | Image Generation Runtime | 対象 | 含める |
| `minimax/speech-2.8-hd` | Speech Synthesis Runtime | 対象 | 含める |
| `minimax/speech-2.8-turbo` | Speech Synthesis Runtime | 対象 | 含める |
| MiniMax ASR | Transcription Runtime | 公開契約を確認できない | 含めない |
| Rapid Voice Cloning | Voice Provisioning | 対象外 | 含めない |
| Voice Design | Voice Provisioning | 対象外 | 含めない |

MiniMax固有の通信、認証、quota確認は共有するが、LLM、画像、TTSのrequest/response契約を1つの巨大なRuntimeへまとめない。

## 3. 公式仕様から確定できる前提

2026-09-06時点のMiniMax公式資料から、次を確認できる。

- Token PlanではSubscription Keyを使用する。
- Subscription Keyは標準PAYG API Keyと別credentialである。
- M3、M2.7、画像、Speechなど、Token Plan対象能力は共通quotaを使用する。
- quotaは5時間rolling windowとweekly windowで管理される。
- `image-01`は`POST /v1/image_generation`で呼び出せる。
- 画像結果はURLまたはbase64で取得でき、Provider URLは24時間で失効する。
- Speech 2.8は`POST /v1/t2a_v2`で同期・streaming TTSを提供する。
- TTSの非streaming結果はhexまたは24時間有効なURLで取得できる。
- Rapid Voice CloningとVoice DesignはToken Planの対象外である。
- Creditsを購入している場合、Subscription Keyは対象能力のsubscription quota消費後にCreditsを使用できる。
- Token Planは個人の対話的な開発用途を想定し、MiniMaxはproduction用途にPAYGを推奨している。
- 公開API一覧には、汎用ASRまたは`audio/transcriptions`相当の正式なendpointがない。

参照:

- [Token Plan Pricing](https://platform.minimax.io/docs/guides/pricing-token-plan)
- [Token Plan FAQ](https://platform.minimax.io/docs/token-plan/faq)
- [API Overview](https://platform.minimax.io/docs/api-reference/api-overview)
- [OpenAI-compatible Text API](https://platform.minimax.io/docs/api-reference/text-openai-api)
- [Text to Image API](https://platform.minimax.io/docs/api-reference/image-generation-t2i)
- [Text to Speech HTTP API](https://platform.minimax.io/docs/api-reference/speech-t2a-http)
- [MiniMax API document index](https://platform.minimax.io/docs/llms.txt)

## 4. 非交渉の設計原則

1. `MINIMAX_TOKEN_PLAN_KEY`だけをsubscription Providerのcredentialとして受け付ける。
2. `MINIMAX_API_KEY`、標準PAYG key、任意base URLは受け付けない。
3. quota超過後にPAYG keyへ切り替えない。
4. quota超過後に別Providerへ自動fallbackしない。
5. Creditsへの移行をsubscription利用として偽装しない。
6. billingを機械的に確認できない場合はfail closedする。
7. MiniMaxをAgent Runtimeへ擬態させない。
8. 既存のlocal Model API、Muse、Grok、Qwen3 Speech daemonを壊さない。
9. Provider固有fieldを共通schemaへ無理に押し込まず、明示的な拡張objectで扱う。
10. 実装はPythonで統一し、MiniMax対応のためにTypeScript bridgeを追加しない。

## 5. スコープ

### 5.1 初回リリースに含める

- Subscription Keyの読み込みとsecret redaction
- 固定した公式global endpointへの接続
- Token Plan status / remainsのpreflight
- key種別、active quota、能力coverageの検証
- MiniMax M3のmodel catalog
- `/v1/chat/completions`
- `/v1/responses`
- text streaming
- tool callの透過・正規化
- `/v1/images/generations`
- Text-to-Image
- base64画像結果
- `/v1/audio/speech`
- `speech-2.8-hd`と`speech-2.8-turbo`
- 非streaming TTS
- TTS streaming
- MiniMaxのsystem voice allowlist
- quota、rate limit、auth、content policy errorの正規化
- 能力別のlocal usage limitと同時実行制限
- health / status / capability catalog
- mock test
- 明示的opt-inのlive smoke test
- Provider運用文書

### 5.2 初回リリースに含めない

- 標準MiniMax Open Platform API Key
- PAYG残高の利用
- PAYGへの自動・手動fallback機能
- Creditsを使うための設定
- Rapid Voice Cloning
- Voice Design
- custom voiceの作成、更新、削除
- 未許可のcustom `voice_id`
- MiniMax ASR
- H3 video generation
- Music generation
- MiniMax CLIのAgent Runtime化
- browser UI automation
- Cookieや非公開tokenの抽出
- image-to-imageの外部URL直接参照
- Provider間routing
- production / multi-user SLA

## 6. 目標構成

```text
                              local-llm
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
          Model API           Media API           Agent API
      chat / responses      image / audio       session / event
              │                   │                   │
      ModelRuntimeRegistry   MediaRuntimeRegistry AgentRuntimeRegistry
              │                   │                   │
       Local       MiniMax   Qwen Speech  MiniMax   Muse / Grok
       daemon      M3       local daemon image/TTS
                     └──────────────┬──────────────┘
                                    │
                         MiniMaxProviderClient
                                    │
                         Subscription Key only
```

Provider credentialとHTTP transportは共有し、公開能力は次のProtocolへ分ける。

```python
class ModelRuntime(Protocol):
    async def list_models(self): ...
    async def chat_completions(self, request): ...
    async def responses(self, request): ...
    async def health(self): ...


class ImageGenerationRuntime(Protocol):
    async def list_models(self): ...
    async def generate(self, request): ...
    async def health(self): ...


class SpeechSynthesisRuntime(Protocol):
    async def list_models(self): ...
    async def synthesize(self, request): ...
    async def stream(self, request): ...
    async def list_voices(self): ...
    async def health(self): ...
```

既存の`TTSBackend`はNumPy waveformを返すローカル推論用の契約である。MiniMaxのencoded audioをその型へ変換してから再encodeする構成にはしない。Gateway-levelの`SpeechSynthesisRuntime`はencoded bytesとcontent typeを扱い、Qwen daemonとMiniMaxを薄いadapterで包む。

## 7. Public API契約

### 7.1 Text

既存のAPIを維持する。

```http
GET  /v1/models
POST /v1/chat/completions
POST /v1/responses
```

`model`が`minimax/`で始まる場合だけMiniMax Model Runtimeへrouteする。Provider prefixを除いたnative model IDをMiniMaxへ送る。

初期allowlist:

```text
minimax/MiniMax-M3
```

`latest`など変動aliasは公開しない。model discoveryが利用できる場合も、Gateway側allowlistとの積集合だけを返す。

### 7.2 Image

```http
POST /v1/images/generations
```

最小request:

```json
{
  "model": "minimax/image-01",
  "prompt": "雨上がりの東京駅を描く",
  "n": 1,
  "response_format": "b64_json",
  "size": "1024x1024"
}
```

初期releaseでは`response_format=b64_json`を必須とする。Providerの24時間URLをそのまま永続URLとして公開しない。Gateway管理URLが必要になった場合は、認証付きartifact storeを別Phaseで追加する。

MiniMax拡張fieldは`minimax` objectへ隔離する。

```json
{
  "minimax": {
    "prompt_optimizer": true,
    "seed": 1234
  }
}
```

`n`、width、height、aspect ratioはProviderの正式な範囲との積集合へ制限する。初期releaseでは外部URLを含む`subject_reference`を公開しない。

### 7.3 TTS

```http
POST /v1/audio/speech
```

最小request:

```json
{
  "model": "minimax/speech-2.8-hd",
  "input": "こんにちは。音声生成のテストです。",
  "voice": "<allowlisted-system-voice-id>",
  "response_format": "mp3",
  "speed": 1.0
}
```

初期allowlist:

```text
minimax/speech-2.8-hd
minimax/speech-2.8-turbo
```

Providerへは`output_format=hex`を指定し、Gatewayがhexを逐次decodeして音声bytesを返す。URL応答は利用しない。

`voice`は公式のsystem voice catalogとGateway allowlistの積集合だけを許可する。custom voiceらしきID、未確認ID、clone/designによって作られたIDは既定で拒否する。

### 7.4 Capability status

既存の`/status`へProviderの能力別状態を追加する。

```json
{
  "id": "minimax-token-plan",
  "billing_mode": "subscription",
  "auth": "verified",
  "quota": {
    "status": "available",
    "remaining": null,
    "reset_at": null,
    "source": "token_plan_remains"
  },
  "capabilities": {
    "text": "available",
    "image_generation": "available",
    "speech_synthesis": "available",
    "transcription": "unsupported",
    "voice_cloning": "not_in_subscription",
    "voice_design": "not_in_subscription"
  }
}
```

公式responseから得られない残量、単位、reset時刻を推測しない。

## 8. Subscription-only gate

### 8.1 Credential

設定名はkeyの用途を明示する。

```dotenv
MINIMAX_TOKEN_PLAN_ENABLED=false
MINIMAX_TOKEN_PLAN_KEY=
MINIMAX_TOKEN_PLAN_REGION=global
```

`MINIMAX_API_KEY`は参照しない。base URLは設定から任意変更できるようにせず、regionごとの公式originをコード側allowlistで固定する。

global初期origin:

```text
Generation: https://api.minimax.io
Quota:      https://www.minimax.io/v1/token_plan/remains
```

### 8.2 Preflight

Runtimeを有効にする前に次を順に検証する。

1. explicit enableがある。
2. Subscription Keyが存在する。
3. secret fileまたは環境変数のpermission条件を満たす。
4. quota endpointが認証に成功する。
5. response schemaがpin済みfixtureと一致する。
6. active Token Plan seatまたはsubscription entitlementを確認できる。
7. text、image、speechの対象能力を確認できる。
8. subscription quotaとCreditsを区別できる。
9. subscription quotaが利用可能である。

どれか1つでも確認できなければ`runtime_billing_unverified`または`runtime_unavailable`とし、生成requestを送らない。

### 8.3 Credits overflowへの対応

MiniMax公式仕様では、Subscription KeyにCreditsが存在する場合、eligibleな利用がCreditsへ移行し得る。

strict subscription-onlyを成立させる条件は次のとおりとする。

- Phase 0でquota APIの実responseからsubscription quotaとCreditsを区別できることを確認する。
- 区別できる場合、各request直前にsubscription quotaを再確認する。
- 区別できない場合、Creditsを持たない専用Team / Subscription Keyを用意し、その状態を機械確認できる方法がない限りRuntimeを有効にしない。
- local usage counterだけをbilling証跡にしない。別clientが同じkeyを使う可能性があるためである。
- quota不足または判定不能時にrequestを試行してProvider errorを待つ設計にしない。

Phase 0で上記を満たせない場合、adapterとmock testまでは実装可能だが、live Runtimeはdisabledのままとする。subscription-onlyを`operator_ack=true`だけで偽装しない。

## 9. 能力別実装方針

### 9.1 MiniMax M3

- OpenAI-compatible Chat Completions / Responses endpointを使用する。
- 既存Pydantic requestから公式互換fieldだけを転送する。
- `priority`などlocal固有fieldをProviderへ送らない。
- tool call IDとargumentsを検証し、provider固有objectを外部schemaへ漏らさない。
- thinking/reasoning fieldはPhase 0でwire responseを確認し、既存Responses eventへ安全に写像できる場合だけ公開する。
- SSE frame、`[DONE]`、usage、finish reasonを厳格に検証する。
- client disconnect時はupstream connectionを閉じる。

### 9.2 image-01

- MiniMaxへは`response_format=base64`を指定する。
- base64 decode前に文字数上限、decode後にbyte数上限を検査する。
- MIME magicを検証し、想定外formatを拒否する。
- 1 requestの`n`を小さく制限する。
- width / height / aspect ratioを公式範囲に制限する。
- prompt長、response総量、timeout、同時実行数を制限する。
- initial releaseではremote reference URLを拒否し、SSRF・tracking・期限切れURL問題を避ける。
- Providerのpartial successは成功画像とfailed countを記録し、全件成功へ偽装しない。

### 9.3 Speech 2.8

- OpenAI互換`input`をMiniMaxの`text`へ写像する。
- `voice`をallowlisted system `voice_id`へ写像する。
- `speed`とaudio formatを公式範囲へclampせず、範囲外は400で拒否する。
- non-streamingはhex decode後のencoded audioをそのまま返す。
- streamingはProvider frame単位でhexをdecodeし、backpressureを保って返す。
- 1 request最大文字数はProvider上限以下に設定する。
- response byte上限、生成timeout、無音・破損audio検査を設ける。
- Providerが返す`usage_characters`、audio length、trace IDはsecretを含まない範囲でmetricsへ保存する。
- `voice_modify`、emotion tag、pronunciation dictionaryは初回releaseでは公開しない。

### 9.4 ASR

MiniMax ASR Runtimeは作らない。`/v1/audio/transcriptions`は既存Qwen3-ASRだけを提供する。

将来追加の条件:

- MiniMax global platformに公開された正式なASR endpointがある。
- request / response schemaと料金対象が公式文書にある。
- Token Plan対象であることを機械確認できる。
- streaming、timestamp、language指定のcapability差を表現できる。

### 9.5 Rapid Voice Cloning / Voice Design

両機能はProvider catalogへ「存在するがsubscription対象外」としてだけ表現し、実行routeを作らない。

該当操作には次を返す。

```json
{
  "error": {
    "code": "capability_not_in_subscription",
    "message": "This capability is not covered by the configured MiniMax Token Plan."
  }
}
```

将来Credits利用を明示的に導入する場合は、`minimax-token-plan`へflag追加せず、credentialとbilling statusが別の`minimax-credits` Providerとして新しい計画を作る。

## 10. 共通MiniMax clientの責務

`MiniMaxProviderClient`は次だけを担当する。

- fixed originへのHTTPS request
- Bearer headerの付与
- connection pool
- connect/read/write timeout
- streaming responseのframe処理
- response byte上限
- JSON schemaの入口検証
- provider trace IDの取得
- secret redaction
- upstream cancel

担当しないもの:

- FastAPI route
- model routing
- capability allowlistの最終判断
- billing policyの最終判断
- OpenAI互換responseの組み立て
- local artifact保存
- Provider間fallback

非同期FastAPIから同期`requests`を直接呼ばない。root依存へ`httpx`を明示的に追加し、1つの`AsyncClient`をlifespanで共有する。

## 11. Error契約

共通error envelope:

```json
{
  "error": {
    "code": "provider_subscription_exhausted",
    "message": "MiniMax Token Plan quota is unavailable.",
    "provider": "minimax",
    "runtime": "minimax-token-plan",
    "retryable": true,
    "details": null
  }
}
```

最低限の正規化:

| Gateway code | HTTP | 条件 |
| --- | ---: | --- |
| `runtime_disabled` | 503 | explicit enableなし |
| `runtime_auth_required` | 401 | keyなし・無効 |
| `runtime_billing_unverified` | 503 | subscription確認不能 |
| `provider_subscription_exhausted` | 429 | subscription quota不足 |
| `provider_rate_limited` | 429 | MiniMax rate limit |
| `capability_not_in_subscription` | 403 | clone/design等 |
| `unsupported_capability` | 400 | ASR等 |
| `invalid_provider_response` | 502 | schema、base64、hex不正 |
| `provider_response_too_large` | 502 | response上限超過 |
| `provider_timeout` | 504 | upstream timeout |
| `provider_unavailable` | 503 | upstream障害 |

Providerの生error body、quota内部値、Authorization headerをcallerへ返さない。retry可能性とreset時刻は公式responseから確定できる場合だけ付与する。

## 12. Securityとprivacy

- Keyをresponse、log、exception、metric labelへ出さない。
- Authorization headerを含むHTTP debug logを無効化する。
- TLS verificationを無効化できる設定を作らない。
- proxy環境変数の継承方針を明示し、意図しないcredential中継を防ぐ。
- arbitrary base URLを禁止する。
- text、prompt、生成画像、音声を既定で永続保存しない。
- observability logにはhash化request ID、model、latency、byte数、usage量だけを残す。
- provider trace IDは長さと文字種を検証する。
- image reference URLは初期releaseで禁止する。
- TTSのcustom voice IDは初期releaseで禁止する。
- voice clone用音声upload endpointは追加しない。
- 生成contentをlogへ含めない。
- FastAPI access token認証をmedia routeにも必須化する。
- local network外へのbindは既存gatewayの認証設定に従う。

## 13. Local quota guard

MiniMaxのquotaは能力間で共有されるため、画像やTTSがM3の利用枠を使い切らないようlocal guardを設ける。

初期設定例:

```dotenv
MINIMAX_MAX_CONCURRENT_TEXT=2
MINIMAX_MAX_CONCURRENT_IMAGE=1
MINIMAX_MAX_CONCURRENT_TTS=1
MINIMAX_MAX_IMAGES_PER_REQUEST=1
MINIMAX_MAX_IMAGE_REQUESTS_PER_DAY=20
MINIMAX_MAX_TTS_CHARACTERS_PER_REQUEST=3000
MINIMAX_MAX_TTS_CHARACTERS_PER_DAY=20000
MINIMAX_PREFLIGHT_TTL_SECONDS=30
```

これらはlocal process内の保護値であり、公式quota残量として表示しない。SQLiteで日次counterを永続化し、process再起動による上限回避を防ぐ。複数process対応は初回対象外とし、MiniMax Runtime有効時は単一workerを前提にする。

## 14. 予定ファイル

```text
providers/
└── minimax/
    ├── __init__.py
    ├── config.py
    ├── client.py
    ├── billing.py
    ├── error_mapping.py
    └── schemas.py

model_runtime/
├── __init__.py
├── base.py
├── registry.py
└── minimax.py

media_runtime/
├── __init__.py
├── base.py
├── registry.py
├── service.py
├── usage.py
├── qwen_speech.py
└── minimax/
    ├── __init__.py
    ├── image.py
    └── tts.py

api/
├── media_schemas.py
└── routes/
    ├── images.py
    └── audio.py

tests/
├── fixtures/
│   └── minimax/
├── test_minimax_config.py
├── test_minimax_billing.py
├── test_minimax_client.py
├── test_minimax_error_mapping.py
├── test_minimax_model_runtime.py
├── test_minimax_image_runtime.py
├── test_minimax_tts_runtime.py
├── test_media_routes.py
└── test_media_usage.py

scripts/
├── check_minimax_token_plan.py
└── smoke_minimax_token_plan.py

docs/providers/
└── minimax-token-plan.md
```

実装で責務の薄いfileが生じる場合は統合してよい。`providers/minimax/`はMiniMax固有transport、`model_runtime/`と`media_runtime/`はGateway契約という依存方向を守る。

## 15. 実装Phase

### Phase 0: 実契約とbilling検証

作業:

- 専用Subscription Keyを用意する。
- keyのglobal regionを確認する。
- `/v1/token_plan/remains`の実responseをsecretを除いてfixture化する。
- active subscription、quota window、Creditsのfieldを特定する。
- text、image、TTS endpointが同じSubscription Keyを受け付けることを確認する。
- system voice catalogの取得方法と最低1つの日本語対応voiceを確認する。
- quota超過、対象外能力、無効keyのerror shapeを確認する。
- image base64、TTS hex、streaming frameの最大実測値を記録する。

テスト:

- 生成を伴わないquota / catalog確認
- 利用者が明示的に許可した最小text 1回
- 1画像
- 短文TTS 1回

完了条件:

- subscription quotaとCreditsを機械的に区別できる。
- 3能力がToken Plan経路で呼ばれた証跡を得られる。
- response / error fixtureからsecretが除去されている。

停止条件:

- subscriptionとCreditsの消費元を区別できない。
- imageまたはTTSが実際にはCreditsを必要とする。
- standard API keyなしでは3能力を利用できない。
- quota endpointの利用条件が自動preflightに適さない。

停止条件に該当した場合は、strict subscription-only Providerとしてのlive実装を進めず、計画を見直す。

### Phase 1: MiniMax共通clientとsubscription gate

作業:

- config、fixed origin、secret loader
- shared `httpx.AsyncClient`
- response size / timeout / redaction
- quota preflightと短いTTL cache
- error mapping
- disabled / unavailable status

テスト:

- key欠落
- malformed key
- auth failure
- quota schema drift
- quota exhaustion
- Credits-only状態
- timeout、5xx、oversized response
- secret redaction

完了条件:

- preflight成功なしに生成requestが送信されない。
- PAYG keyを与えてもRuntimeが有効にならない。
- 既存APIはMiniMax無効時も起動する。

### Phase 2: Model Runtime coreとMiniMax M3

作業:

- `ModelRuntime`最小Protocol
- local daemon adapter
- MiniMax M3 adapter
- model prefix routing
- Chat Completions / Responses
- streaming、tool call、usage正規化

テスト:

- local modelの既存契約回帰
- unknown / disallowed model
- MiniMax request mapping
- streaming cancellation
- malformed SSE
- tool call arguments
- finish reasonとusage
- quota途中枯渇

完了条件:

- local modelの既存testが変更なく通る。
- `minimax/MiniMax-M3`だけがMiniMaxへrouteされる。
- Provider固有fieldが既存公開契約を壊さない。

### Phase 3: Speech Synthesis Runtime

作業:

- provider-neutral TTS request / result
- Qwen daemon adapter
- MiniMax Speech 2.8 adapter
- system voice allowlist
- hex decode
- streaming backpressure
- response format mapping

テスト:

- Qwen3 TTS既存route回帰
- HD / Turbo routing
- voice allowlist
- custom voice拒否
- clone / design拒否
- invalid hex
- empty audio
- oversized audio
- client disconnect
- character limit

完了条件:

- 既存Qwen TTSの利用方法を維持する。
- system voiceによるMiniMax TTSだけが実行できる。
- Voice Cloning / Designへのnetwork requestが発生しない。

### Phase 4: Image Generation Runtime

作業:

- OpenAI-compatible image request / response schema
- MiniMax `image-01` mapping
- base64 validation
- size / aspect ratio mapping
- partial success処理
- image concurrency / daily counter

テスト:

- prompt validation
- model allowlist
- invalid size / `n`
- URL response拒否
- remote reference拒否
- malformed base64
- unexpected MIME
- partial / total failure
- response byte上限

完了条件:

- 初期releaseでProvider URLをcallerへ返さない。
- `image-01`以外を呼べない。
- 大きすぎるresponseをmemoryへ展開し続けない。

### Phase 5: Usage guardとobservability

作業:

- 能力別local counter
- quota refresh
- metrics
- `/status` capability表示
- structured log
- model / image / TTSのactive request数

テスト:

- 日次境界
- process再起動後のcounter復元
- concurrency上限
- quota cache expiry
- stale quota時のfail closed
- content / secretがlogへ出ないこと

完了条件:

- local counterと公式quotaを明確に区別して表示する。
- image / TTSの大量利用を設定上限で止められる。

### Phase 6: 運用文書とlive smoke

作業:

- Provider導入手順
- Subscription Keyの作成・保管
- Creditsなし専用keyの条件
- supported / unsupported capability
- quota枯渇時の挙動
- troubleshooting
- opt-in smoke script
- rollback手順

live smokeは次の明示的flagを必須とする。

```text
MINIMAX_LIVE_TESTS=1
MINIMAX_LIVE_TEST_ACKNOWLEDGE_SUBSCRIPTION_USAGE=1
```

完了条件:

- 通常testでは外部通信もsubscription消費も発生しない。
- live smokeは使用model、能力、quota sourceを表示してから実行する。
- smoke後にquota statusを再取得する。

## 16. Test matrix

| 分類 | mock | live |
| --- | --- | --- |
| Config / secret | 必須 | 不要 |
| Billing preflight | 必須 | 必須 |
| Text non-stream | 必須 | 1回 |
| Text stream | 必須 | 任意 |
| Tool call | 必須 | 任意 |
| Image base64 | 必須 | 1画像 |
| TTS non-stream | 必須 | 短文1回 |
| TTS stream | 必須 | 任意 |
| Quota exhaustion | fixture必須 | 実施しない |
| Credits overflow拒否 | fixture必須 | Creditsなしkeyで確認 |
| Voice Clone / Design拒否 | 必須 | 外部call禁止 |
| ASR unsupported | 必須 | 外部call禁止 |
| Existing local APIs | 必須 | 不要 |
| Muse / Grok Agent APIs | 必須 | 不要 |

すべてのlive testはsubscription quotaを消費するため、通常CIと`pytest` default markerから除外する。

## 17. Rolloutとrollback

Rollout:

1. default disabledでreleaseする。
2. mock testと全既存testを通す。
3. check scriptでpreflightだけを行う。
4. dedicated keyでtextだけを有効にする。
5. TTSを有効にする。
6. imageを有効にする。
7. 数日間quotaとerror rateを観測する。

Rollback:

- `MINIMAX_TOKEN_PLAN_ENABLED=false`で全MiniMax能力を停止する。
- capability単位のenable flagでimage / TTSだけを停止できるようにする。
- MiniMax停止中もlocal model、Qwen Speech、Muse、Grokを起動可能にする。
- state migrationは追加tableだけで行い、rollback時に既存tableを削除しない。

## 18. 初回releaseの完了条件

- Subscription Key以外のcredentialを使用しない。
- subscription quotaを機械的に確認してからrequestを送る。
- Creditsへの移行可能性がある状態ではrequestを送らない。
- PAYGおよびProvider fallbackを実装していない。
- M3、image-01、Speech 2.8だけをallowlistから利用できる。
- ASR、Rapid Voice Cloning、Voice Designを実行できない。
- text streamingとtool callが既存契約へ正規化される。
- image URLを永続結果として公開しない。
- TTSのcustom voiceを既定で拒否する。
- local usage guardと公式quota statusを区別できる。
- secrets、prompt、画像、音声がlogへ出ない。
- MiniMax無効・未契約・quota枯渇・障害時も既存Runtimeを利用できる。
- mockを含む全testと、明示的opt-inの最小live smokeが成功する。

## 19. 実装開始前に残る確認事項

次はPhase 0で実物を確認するまで確定しない。

- `/v1/token_plan/remains`の現在のresponse schema
- subscription quotaとCredits残量を区別するfield
- capability別entitlementの表現
- quota消費直前・直後の更新遅延
- image / TTS requestがSubscription Keyで成功したときのbilling表示
- M3 Responses APIのstream event shape
- Speech 2.8のstreaming frame境界
- 日本語向けsystem voice IDの正式なallowlist
- quota不足、対象外能力、content policy違反のerror code
- Team用Subscription KeyでCredits利用を無効化できる管理設定の有無

これらを推測で実装しない。特にCreditsとの区別が解決しない場合、MiniMax Token Plan Providerはstrict subscription-only要件を満たさないため、live有効化を停止する。
