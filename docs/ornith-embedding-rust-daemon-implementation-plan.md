# Ornith・Embedding Rust 常駐デーモン実装計画書

作成日: 2026-08-31

外部依存確認日: 2026-08-31

対象環境: Apple Silicon Mac（M4、32GBユニファイドメモリ、arm64）

状態: 実装・実機検証中

## 実装時の確定差分（2026-08-31）

- Embedding は FP32 との golden 比較後、arm64 上で動作確認した公式 qint8
  ONNX を常駐構成に採用した。4 fixture の最小 cosine similarity は
  `0.999286`、実測 physical footprint は 601 MiB。FP32 は rollback 用に残す。
- desired state は既存 SQLite の schema 変更を避け、ContextStill の run directory
  に atomic rename で保存する `local-model-desired.json` を採用した。CLI、resident
  supervisor、`ornithd`の自動復帰処理が同じファイルを読み書きする。
- control socket は追加せず、常駐する `context-stilld` の5秒 reconcile と health probe
  で反映する。worker の PID と起動引数は既存 ProcessState repository を利用する。
- `ornithd` は API を再実装せず、artifact 検証後に Metal 専用ビルドの
  mistral.rs 0.9.2を子processとして監督する薄いRust launcherとした。これにより
  signal転送と128K利用後の32K再起動をPythonなしで行う。
- 実機では `standard` の BF16 KV cache が 32,768 tokens、`long` の F8E4M3
  KV cache が 131,072 tokens として確保されたことを起動ログで確認し、両方で
  Chat Completions の生成を完了した。最終常駐状態は `standard` とする。

## 0. 決定事項

この計画では、次を変更しない前提として実装する。

| 項目 | 決定 |
|---|---|
| 運用方式 | オンデマンドロードではなく常時起動・常時モデルロード |
| 停止方式 | 明示的な`start`、`stop`、`restart`、`status`を提供 |
| supervisor | 既存のRust製`context-stilld`を唯一の親プロセスとする |
| LLM worker | Rust製`ornithd`、mistral.rs + Candle + Metal |
| Embedding worker | Rust製`embeddingd`、fastembed-rs + ONNX Runtime |
| プロセス分離 | LLMとEmbeddingは別プロセス、同じサービスグループとして管理 |
| Python | 移行試験中だけ比較用に残し、最終運用では依存も常駐も0にする |
| Swift/iOS native | 採用しない |
| LLM標準profile | `standard`、32,768 tokens |
| LLM長文profile | `long`、131,072 tokens |
| profile切替 | `ornithd`だけを再起動し、`embeddingd`は継続稼働 |
| MTP | `auto` / `on` / `off`。未評価の`auto`は安全側の`off` |
| 外部API | 現行portと主要endpointを維持 |
| launchd | worker個別のLaunchAgentは作らず、`context-stilld`だけを登録 |
| 画像入力 | 初期リリースの非目標。テキストAPIを先に置換 |

## 1. 結論

`local-llm`へCargo workspaceを追加し、次の2バイナリを実装する。

1. `ornithd`
   - `Ornith-1.0-9B`をMetalへ常駐ロードする。
   - OpenAI互換のChat CompletionsとResponses APIを提供する。
   - 32Kと128Kのcontext profileを起動時に選ぶ。
   - MTPはmistral.rsの組み込み実装だけを使用する。
2. `embeddingd`
   - `intfloat/multilingual-e5-small`のONNXモデルを常駐ロードする。
   - 現行`/embed`とOpenAI互換`/v1/embeddings`を提供する。

`context-stilld`が両workerのdesired state、起動、停止、再起動、health、crash recoveryを管理する。worker自身はdaemonizeせずforegroundで動き、親からSIGTERMを受けて終了する。

## 2. 目的

- 現在のPython製Ornith APIとEmbedding daemonをRustへ置き換える。
- モデルを毎回ロードせず、`start`後は常に即時利用可能にする。
- `stop`後はworkerプロセスを完全に終了し、Metal・ONNX Runtimeのメモリを解放する。
- 32GB Macで通常利用を32Kへ抑え、必要なときだけ128Kへ切り替える。
- 現在のクライアント設定、port、認証、主要response schemaを壊さない。
- Python runtime、virtualenv、sentence-transformers、mlx-vlmを最終的な運用経路から除く。
- 実測に基づいてMTPを有効化し、問題時は自動的にMTPなしへ縮退する。

## 3. 非目標

- workerをリクエストごとに起動・停止しない。
- idle timeoutによるmodel unloadを実装しない。
- 128K workerを32K workerと同時常駐させない。
- context上限をリクエストごとに動的変更しない。
- mistral.rsのMTP、Qwen3.5 loader、Metal kernelを独自再実装しない。
- 初期リリースで画像・動画入力を公開しない。
- 外部ネットワークへAPIを公開しない。
- workerへツール実行機能を持たせない。tool callの実行責務はクライアント側に残す。
- ASR、TTS、tauri-plugin-llm-fetchのプロセス管理をこの移行へ含めない。
- Python版とRust版を同じportで同時起動しない。

## 4. 現行基盤と置換範囲

### 4.1 現行LLM API

- 実装: FastAPI、Uvicorn、mlx-vlm
- port: `127.0.0.1:44448`
- endpoint:
  - `GET /health`
  - `GET /status`
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/responses`
- MTP: 現在の`.env`と`.env.example`はいずれも`GEMMA4_MTP_ENABLED=false`
- context: 現在の`.env`は172,000、`.env.example`と起動scriptの既定は176,000
- queue: 単一推論workerとpriority queue
- 認証: `LOCAL_LLM_REQUIRE_AUTH`と`LOCAL_LLM_ACCESS_TOKEN`

### 4.2 現行Embedding daemon

- 実装: Python stdlib HTTP server、sentence-transformers
- model: `intfloat/multilingual-e5-small`
- port: `127.0.0.1:44512`
- endpoint:
  - `GET /health`
  - `GET /status`
  - `POST /embed`
  - `POST /v1/embeddings`
- 入力prefix: `query: `または`passage: `
- pooling: mean pooling
- 標準出力: L2正規化済み384次元vector

### 4.3 置換しない契約

- port番号
- Bearer認証の有効・無効条件
- endpoint path
- `tool_choice`、tool argument validation、streamingの既存契約
- `query:` / `passage:` prefix
- Embeddingの既定L2正規化
- クライアントが指定する既存model alias

既存契約に曖昧な箇所がある場合は、Phase 0でPython版のgolden fixtureを正本として固定してからRust側を実装する。

## 5. 目標構成

```text
launchd
  └─ context-stilld                         Rust / 唯一のsupervisor
       ├─ local-model config + desired state
       ├─ lifecycle CLI / IPC
       ├─ health aggregation
       ├─ crash recovery
       │
       ├─ ornithd                           Rust
       │    127.0.0.1:44448
       │    Axum + Tokio
       │    mistral.rs v0.9.2 + Candle + Metal
       │    Ornith-1.0-9B UQFF Q4K
       │
       └─ embeddingd                        Rust
            127.0.0.1:44512
            Axum + Tokio
            fastembed-rs + ONNX Runtime
            multilingual-e5-small FP32 ONNX
```

2 workerを分ける理由は、次の通り。

- OrnithのMetal障害がEmbeddingへ波及しない。
- profile変更時にEmbeddingを止めずに済む。
- worker終了時に各runtimeのメモリをOSへ確実に返せる。
- crash回数、backoff、health、logをサービス単位で扱える。

## 6. 所有権と責務

### 6.1 `context-stilld`

- 永続設定とdesired stateの正本
- worker binary、model artifact、portの解決
- spawn、SIGTERM、SIGKILL、PID追跡
- 起動順序、readiness待機、restart backoff
- profileとMTP modeの変更
- `local-model` CLIの受付
- 2 workerをまとめたstatusの返却

### 6.2 `ornithd`

- Ornith artifactの整合性検証
- tokenizer、chat template、modelのロード
- token countとcontext budget検証
- generation、streaming、cancel
- tool callとreasoningのresponse変換
- worker自身のhealth、metrics、graceful shutdown

### 6.3 `embeddingd`

- ONNX artifactとtokenizerの整合性検証
- tokenization、mean pooling、L2 normalize
- `/embed`と`/v1/embeddings`のschema変換
- worker自身のhealth、metrics、graceful shutdown

### 6.4 クライアント

- tool callの実行
- 32Kに収まらない履歴の要約・圧縮
- `context_profile_required`を受けた場合の128K切替要求
- 128K推論完了後の自動`standard`復帰を妨げないrequest lifecycle

## 7. 技術スタックとversion固定

### 7.1 共通

| 依存 | 採用 | 固定方法 |
|---|---|---|
| Rust | 1.98.0、edition 2024 | `rust-toolchain.toml`へversion固定 |
| HTTP | Axum | `Cargo.lock`固定 |
| async | Tokio | `Cargo.lock`固定 |
| schema | Serde、serde_json | `Cargo.lock`固定 |
| error | thiserror、anyhow | library境界はtyped error |
| tracing | tracing、tracing-subscriber | JSON line log |
| metrics | metrics + Prometheus exporter | localhostのみ |
| shutdown | tokio-util CancellationToken | signalから全taskへ伝播 |

### 7.2 Ornith

- `mistralrs` crateをGit dependencyとして使用する。
- tagだけでなくcommit `d184053f2441f897cf81429b98b0d868f4d96ff3`へ固定する。
- featureは`metal`だけを必須とし、CUDA、MKL、code executionを含めない。
- 高水準の`UqffMultimodalModelBuilder`を使用し、独自loaderを書かない。
- MTPは`MtpConfig::builtin`経路だけを使用する。

Cargo指定の基準:

```toml
mistralrs = { git = "https://github.com/EricLBuehler/mistral.rs.git", rev = "d184053f2441f897cf81429b98b0d868f4d96ff3", default-features = false, features = ["metal"] }
```

### 7.3 Embedding

- `fastembed = "=5.17.4"`へ固定する。
- `UserDefinedEmbeddingModel`でローカル`onnx/model.onnx`を直接読む。
- poolingは`Pooling::Mean`を明示する。
- fastembed-rsの自動model downloadは使わない。
- 初期版はONNX Runtime CPU execution providerを正本とする。
- CoreML execution providerはPhase 7の比較対象に留め、parityとメモリの両方を満たした場合だけ採用する。

`fastembed-rs 5.17.4`は`ort 2.0.0-rc.13`を使用する。`Cargo.lock`とartifact manifestを同時にレビュー対象にする。

## 8. Model artifact

### 8.1 Ornith

現在使用している`mlx-community/Ornith-1.0-9B-4bit`はMLX用4bit形式であり、mistral.rsへそのまま渡さない。

変換元:

- model: `ornith-ai/Ornith-1.0-9B`（旧ID `deepreinforce-ai/Ornith-1.0-9B`からredirect）
- source revision: `83dc1f5e24ef8527af019a6b3bf66ac0f1c2c999`
- architecture: `Qwen3_5ForConditionalGeneration`
- model context上限: 262,144 tokens
- hidden size: 4,096
- MTP hidden layer: 1

配布artifact:

```text
models/ornith-1.0-9b-uqff-q4k/
  q4k-0.uqff
  residual.safetensors
  config.json
  generation_config.json
  chat_template.jinja
  tokenizer.json
  tokenizer_config.json
  processor_config.json
  preprocessor_config.json
  video_preprocessor_config.json
  model-manifest.json
```

artifact生成はruntime起動時に行わない。固定revisionを事前取得したlocal directoryに対し、次を一度だけ実行する。

```bash
mistralrs quantize multimodal \
  --model-id /absolute/path/to/ornith-source \
  --isq q4k \
  --output /absolute/path/to/ornith-uqff
```

全fileのSHA-256、byte数、source revision、converter commit、commandをmanifestへ記録する。変換jobはメモリ64GiB以上の専用hostまたはCI runnerで実行し、32GBの運用Macへ変換負荷を持ち込まない。

初期リリースではQwen3.5 multimodal loaderを使用するが、API入力はtextだけに制限する。vision weightを除く最適化は、同一出力とloader互換性を実証するまで実施しない。

### 8.2 Embedding

取得元は`intfloat/multilingual-e5-small`のrevision `614241f622f53c4eeff9890bdc4f31cfecc418b3`へ固定する。初期artifactは公式`onnx/model.onnx`を使用する。`model_qint8_avx512_vnni.onnx`はarm64対象ではないため使用しない。

```text
models/multilingual-e5-small-onnx-fp32/
  onnx/model.onnx
  config.json
  tokenizer.json
  tokenizer_config.json
  special_tokens_map.json
  sentencepiece.bpe.model
  model-manifest.json
```

Embedding modelは最大512 tokensで切り、現行sentence-transformersと同じtruncation、padding、mean pooling、L2 normalizeを再現する。FP16、INT8、CoreMLはFP32 parity完了後の候補とする。

実体fileはGitへcommitせず、`~/Library/Application Support/local-llm/models/`へinstallする。Gitで管理するのはmanifestと取得・検証手順だけとする。

### 8.3 Manifest検証

workerはHTTP bind前に次を確認する。

1. manifest schema version
2. model IDとrevision
3. 必須fileの存在
4. byte数
5. SHA-256
6. runtime互換version

不一致時は自動downloadや自動変換を行わず、exit code `78`で終了する。`context-stilld status`には`artifact_invalid`と対象fileを表示する。

## 9. 常駐状態モデル

desired stateはworkerごとに`running`または`stopped`を持つ。初回install時の既定は両方`running`とする。

```text
stopped ── start ──> starting ── ready ──> running
   ▲                    │                    │
   │                    └─ error ─> failed ─┤ restart/backoff
   │                                        │
   └────────────── stop <── stopping <──────┘
```

規則:

- `desired=running`で異常終了した場合だけ自動再起動する。
- `desired=stopped`は再起動、login、reconcileを越えて維持する。
- `stop`はsignal送信より先にdesired stateを`stopped`へ保存する。
- `start`はdesired stateを`running`へ保存してからspawnする。
- supervisor再起動時はdesired stateに従ってreconcileする。
- 同一workerの二重起動をPIDだけで判断せず、PID start timeとloopback port ownershipも確認する。

### 9.1 Start sequence

`start all`は次の順で行う。

1. configとartifactを検証する。
2. 両workerのdesired stateを`running`としてatomic保存する。
3. `embeddingd`をspawnする。
4. `/health`がreadyになるまで最大30秒待つ。
5. `ornithd`をspawnする。
6. `/health`がreadyになるまで最大90秒待つ。
7. 両方readyの場合だけcommandを成功にする。

途中失敗時もdesired stateは`running`のままとし、supervisorがbackoff再試行する。CLIは成功扱いにせず、失敗workerと直近errorを返す。

### 9.2 Stop sequence

`stop all`は次の順で行う。

1. 両workerのdesired stateを`stopped`としてatomic保存する。
2. `ornithd`、`embeddingd`の順にSIGTERMを送る。
3. 各workerはsignal handlerでreadyをfalseにし、新規受付を停止する。
4. 各workerは実行中requestを最大10秒drainして自発終了する。
5. supervisorはSIGTERMから最大20秒待つ。
6. 残存processだけへSIGKILLを送る。
7. PID・runtime statusをclearする。
8. process不在、port close、health接続失敗を確認する。

commandの全体上限は35秒とする。SIGKILL使用時は終了自体を成功とし、`forced=true`を返してlogへ残す。

### 9.3 Crash recovery

backoffは1、2、4、8、16、30秒、以降30秒とする。10分正常稼働したら連続失敗回数を0へ戻す。5分間に5回起動失敗したworkerは`failed`へ移し、自動再試行を5分間停止する。手動`restart`はこのcooldownを解除する。

## 10. 永続設定とruntime state

永続するoperator設定は、既存のSQLite runtime settings document（namespace `runtime`、key `settings.v1`）へ保存する。新しい設定fileを正本にせず、`settings.localModel`を追加する。

```json
{
  "settings": {
    "localModel": {
      "schemaVersion": 1,
      "services": {
        "ornith": { "desiredState": "running" },
        "embedding": { "desiredState": "running" }
      },
      "ornith": {
        "configuredProfile": "standard",
        "mtpMode": "auto"
      }
    }
  }
}
```

更新は既存SQLite writerへ単一transactionとして依頼し、read-modify-writeの競合をversionで検出する。CLI processがSQLiteを直接更新しない。

```text
~/Library/Application Support/contextStill/
  local-model/mtp-benchmark.json
  run/ornithd.json
  run/embeddingd.json
  logs/ornithd.jsonl
  logs/embeddingd.jsonl
  run/local-model-control.sock
```

`temporaryProfile`とMTP degraded overrideはresident processのmemoryと`run/ornithd.json`だけに置き、永続settingsへ書かない。supervisor再起動時に必ず消える。`run/*.json`は観測値であり、desired stateの正本にしない。最低限、PID、process start time、binary version、artifact revision、bind address、startedAt、readyAt、lastExitを記録する。

secretはSQLite settings、runtime state、benchmark fileへ保存しない。既存のaccess token解決方法を使い、statusとlogではredactする。

## 11. CLI契約

CLIは`context-stilld`へ追加する。対象省略時は`all`とする。

```bash
context-stilld local-model start [all|ornith|embedding]
context-stilld local-model stop [all|ornith|embedding]
context-stilld local-model restart [all|ornith|embedding] [--profile standard|long] [--temporary]
context-stilld local-model status [--json]
context-stilld local-model profile get
context-stilld local-model profile set standard|long [--temporary]
context-stilld local-model mtp get
context-stilld local-model mtp set auto|on|off
context-stilld local-model benchmark-mtp [--profile standard] [--repeats 5]
context-stilld local-model doctor
```

規則:

- CLIは`run/local-model-desired.json`をatomic renameで更新し、workerを直接spawnしない。
- residentは最大5秒のreconcileでdesired stateを反映する。resident不在時はexit code 5を返す。
- `start`はreadyまで、`stop`はprocessとportの消滅まで待つ。
- `profile set`は設定保存後、`ornithd`だけをrestartする。
- `long`はcompleted sequenceを観測するまで自動解放しない。推論完了後、in-flight
  requestとwaiting sequenceが0の状態が30秒続いた時点でdesired profileを
  `standard`へ更新し、`ornithd`だけを32K設定でrestartする。猶予中の新規requestは
  timerをresetする。
- `--temporary`は現在のsupervisor session中だけ有効で、次のsupervisor再起動時にconfigured profileへ戻す。
- `mtp set`も`ornithd`だけをrestartする。
- `stop ornith`中もEmbeddingは利用可能とする。
- 同じ状態への`start` / `stop`は成功する冪等commandとする。
- `status --json`は自動化用のstable schemaとし、human表示の文言に依存させない。

終了code:

| code | 意味 |
|---:|---|
| 0 | 要求状態へ到達 |
| 2 | 引数・設定不正 |
| 3 | 一部workerだけ到達 |
| 4 | timeout |
| 5 | supervisorへ接続不能 |
| 6 | artifact不正 |

### 11.1 Worker起動契約

residentはproductionで次の形に正規化してspawnする。workerは`auto`を解釈せず、supervisorが解決したeffective値だけを受け取る。

```bash
ornithd \
  --mistralrs-bin "/absolute/path/to/mistralrs" \
  --model-file "/absolute/path/to/ornith-1.0-9b-Q4_K_M.gguf" \
  --manifest "/absolute/path/to/ornith-manifest.json" \
  --host 127.0.0.1 \
  --port 44448 \
  --profile standard \
  --long-idle-seconds 30 \
  --desired-state-file "/absolute/path/to/local-model-desired.json"

embeddingd \
  --host 127.0.0.1 \
  --port 44512 \
  --model-dir "/absolute/path/to/multilingual-e5-small-onnx-qint8" \
  --manifest "/absolute/path/to/embedding-manifest.json" \
  --request-timeout-seconds 30
```

MTP onの場合だけ`--mtp --mtp-n-predict 2|3`を追加する。Bearer tokenをprocess argumentへ入れず、既存の`LOCAL_LLM_REQUIRE_AUTH`と`LOCAL_LLM_ACCESS_TOKEN`をresidentから継承する。非secret設定は再現可能性のためargumentへ明示する。

worker exit codeは0=正常終了、2=Clapによる引数不正、1=artifact・runtime・内部初期化失敗とする。詳細原因はstderrへ出し、signal終了はruntime stateへsignal名を別記録する。

## 12. Context profile

| profile | context window | reserved output | safe prompt上限 | 用途 |
|---|---:|---:|---:|---|
| `standard` | 32,768 | 4,096 | 28,672 | 通常常駐、既定 |
| `long` | 131,072 | 8,192 | 122,880 | 長い調査・大規模code context |

model自体の上限262,144は公開profileとして使用しない。32GB環境での余裕と他daemon共存を優先する。

profileは`ornithd`起動時のKV cache・PagedAttention設定へ反映する。mistral.rs v0.9.2の`with_max_model_len`はQwen3.5 multimodal loaderを対象にしていないため使用しない。`PagedAttentionMetaBuilder`へ`MemoryGpuConfig::ContextSize(profile.context_window)`を渡してcacheを制限し、API層でも同じprofile上限を検証する。PagedAttentionのcache budgetは起動時に決まるため、profile変更は必ずworker restartとする。

`long`の自動解放はwall-clockだけで判断しない。mistral.rsの`/metrics`から
`http_requests_in_flight`、`mistralrs_sequences_waiting`、
`mistralrs_sequences_completed_total`をlocalhost経由で監視する。completed counterが
増えた場合だけtask完了済みとして自動解放をarmする。エラー応答など推論を完了して
いないHTTP requestだけではarmしない。以降、in-flight requestとwaiting sequenceが
ともに0の状態が既定30秒継続したら、desired stateの`standard`更新、128K childの
graceful shutdown、32K childの起動を同じ自動復帰処理で行う。
短いrequestをpoll間隔の間に取りこぼしてもcompleted counterで検知できる。

```rust
let paged = PagedAttentionMetaBuilder::default()
    .with_block_size(32)
    .with_gpu_memory(MemoryGpuConfig::ContextSize(profile.context_window))
    .build()?;
```

model metadataの262,144は改変しない。workerが公開する上限とPagedAttention cacheだけをprofileへ合わせる。

概算では、全attention layer相当の単純BF16 KV換算は32Kで約1GiB、128Kで約4GiBとなる。ただしOrnithはlinear/full attentionのhybridであり、実際のallocated・resident・peakはmistral.rsとMetalの実測値を正本とする。

### 12.1 超過時の応答

workerはchat template適用後のtoken数を計測する。`max_tokens` / `max_output_tokens`の既定は現行どおり1,024、上限は`standard=4,096`、`long=8,192`とする。実際の入力予算は`context window - requested output`で計算し、表のsafe prompt上限はprofile最大出力を予約した保守値である。既存の安全な機械圧縮を適用しても予算を超える場合、truncateや自動restartを行わずHTTP 400を返す。

```json
{
  "error": {
    "code": "context_profile_required",
    "message": "The request exceeds the active context profile.",
    "activeProfile": "standard",
    "contextWindowTokens": 32768,
    "requiredContextWindowTokens": 48120,
    "recommendedProfile": "long",
    "restartRequired": true
  }
}
```

131,072を超えるrequestは`context_budget_exceeded`とし、`recommendedProfile`は返さない。

## 13. HTTP API契約

### 13.1 共通

- bindは`127.0.0.1`固定。`0.0.0.0`は起動時validationで拒否する。
- `/health`と`/status`は現行どおり認証なしとし、本文やsecretを含めない。
- 推論endpointは現行Bearer認証契約を維持し、`/metrics`は認証必須とする。
- request body上限はLLM 16MiB、Embedding 2MiBとする。
- request IDは`x-request-id`を受け入れ、未指定時はUUID v7を生成する。
- errorは`error.code`、`error.message`、`requestId`を必ず持つ。

### 13.2 Ornith

- `GET /health`
- `GET /status`
- `GET /metrics`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`

公開model aliasは現行の`ornith-1.0-9b-4bit`を維持する。内部artifactがUQFF Q4Kへ変わってもclientへ新しいmodel IDを要求しない。statusだけにbackend formatとartifact revisionを追加する。

Qwen系profileは現行どおりthinking無効を既定とする。既存の`LOCAL_LLM_ENABLE_THINKING=true`を明示した場合だけ`<think>`部分を通常本文から分離し、Chat Completionsでは`reasoning_content`、Responsesではreasoning itemへ変換する。生の`<think>` tagを`content`へ残さない。

初期版は1 generationだけをactiveにし、`high`、`normal`、`low`のpriority queueへ入れる。queue上限は8、同一priority内はFIFO、queue timeoutはrequest timeoutに含める。mistral.rsのcontinuous batchingは、現行contractとmemory上限を実測後に別変更で有効化する。

streamingはSSEを使用し、切断をCancellationTokenからmistral.rs requestへ伝播する。切断済みrequestの生成を継続しない。

tool callは現行TypeScript/Python側の期待schemaをPhase 0 fixtureで固定し、次を維持する。

- `tool_choice: none`ではtool schemaをmodelへ渡さない。
- `tool_choice: required`でtool callがない場合は`required_tool_call_missing`。
- argumentsをJSON Schemaの既存subsetで検証する。
- streaming tool callの`finish_reason`は`tool_calls`。
- 初期版はparallel tool callを非対応とする。

### 13.3 Embedding

`POST /embed`:

```json
{
  "texts": ["本文"],
  "type": "passage",
  "normalize": true,
  "priority": "normal"
}
```

`POST /v1/embeddings`はstringまたはstring arrayを受ける。既存互換のためこのendpointでは`query:` prefixとL2 normalizeを使用する。token ID array入力は初期版で400とする。

`/embed`は現行の`embeddings`、`dimension`、`count`、`type`、`normalize`、`queueWaitMs`、`encodeMs`を維持する。`/v1/embeddings`の`usage`も初期移行では現行どおりUTF-8文字列長を4で割る概算を維持し、tokenizer実測への変更は別のversioned contract変更とする。

batch上限は64件、1件512 tokens、合計raw UTF-8 1MiBとする。空文字、空配列、非stringは400、queue満杯は429、timeoutは503を返す。

## 14. Ornith worker実装

起動処理:

1. CLI引数と環境変数をparseする。
2. localhost bind、profile、MTP modeをvalidateする。
3. manifestと全artifact hashを検証する。
4. `UqffMultimodalModelBuilder`をQ4K shardへ向ける。
5. Metal device、`MemoryGpuConfig::ContextSize`、API context上限を設定する。
6. effective MTPがonなら`MtpConfig::builtin(Some(n_predict))`を設定する。
7. modelをloadし、短い固定promptでwarmupする。
8. HTTP serverをbindする。
9. `/health.ready=true`へ遷移する。

内部moduleはHTTP schema、provider contract、model adapter、queue、context budget、streamingを分離する。mistral.rsの型をAPI schemaへ直接露出させず、`OrnithEngine` traitを境界にする。

```rust
#[async_trait]
pub trait OrnithEngine: Send + Sync {
    async fn count_tokens(&self, request: &NormalizedChatRequest) -> Result<TokenBudget>;
    async fn generate(&self, request: GenerationRequest) -> Result<GenerationResult>;
    async fn stream(&self, request: GenerationRequest) -> Result<GenerationStream>;
    async fn shutdown(&self) -> Result<()>;
}
```

fake engineはunit/integration testだけで使用し、production featureへ含めない。

## 15. Embedding worker実装

起動時に`UserDefinedEmbeddingModel`へONNX、tokenizer files、`Pooling::Mean`を渡す。1本のblocking inference workerを専用threadで動かし、Axum task上でONNX推論を実行しない。

fastembed-rsの高水準`TextEmbedding::embed`は出力を常にL2 normalizeするため、`normalize=false`を再現できない。実装は`TextEmbedding::transform`とcustom output transformerを使い、mean pooling後にrequestが`normalize=true`のときだけL2 normalizeする。両分岐をPython golden vectorと比較する。

処理順:

1. request validation
2. `query: ` / `passage: ` prefix付与
3. tokenizerで512 tokensへtruncate
4. batch化
5. ONNX推論
6. attention maskによるmean pooling
7. 要求時だけL2 normalize
8. response schemaへ変換

queueはpriority 3段、上限32 job、active worker 1とする。batch内64件とjob concurrencyを混同しない。shutdown時は新規受付を止め、実行中ONNX callを最大10秒待ち、process終了でsession memoryを解放する。

## 16. MTP方針

MTPのためにPythonを残さない。mistral.rs v0.9.2はQwen3.5のbuilt-in MTP、hybrid model修正、quantized drafter、batched device verification、GDN rollback、model sizeに応じた`n_predict`を含むため、これを利用する。

設定の意味:

| mode | effective |
|---|---|
| `off` | 常にMTPなし |
| `on` | 常にMTPあり。load失敗時はworker起動失敗 |
| `auto` | 有効なbenchmark結果があればその結果、なければoff |

Ornithのhidden sizeは4,096なので、比較対象は`n_predict=2`と`3`とする。`4`は初期評価対象に含めない。

### 16.1 Benchmark

`benchmark-mtp`は`standard` profileで次を同一条件比較する。

- warmup 2回
- 日本語、英語、code、tool callを各5prompt
- 各promptを5回
- greedy、max output 256
- MTP off、on/n=2、on/n=3
- TTFT、decode tokens/s、total latency、peak physical memory、acceptance rate

`auto=on`の合格条件:

- MTP off/onでgreedy出力token IDが全fixture一致
- tool call nameとargumentsが一致
- stream event順序とfinal usageが一致
- 中央値decode速度がoff比10%以上改善
- p95 total latencyがoff比で悪化しない
- peak physical memoryがprofile予算内
- 100連続requestでpanic、Metal error、hangが0

最速の合格候補を`mtp-benchmark.json`へruntime commit、artifact hash、macOS build、hardware ID付きで保存する。いずれも不合格なら`auto`のeffectiveはoffとする。

### 16.2 自動縮退

effective MTP onで、5分以内にMTP・speculative decode・GDN rollback関連の異常終了が3回発生した場合、supervisorは次回起動だけMTP offへoverrideし、状態を`running_degraded`とする。desired modeは書き換えない。`restart`または再benchmarkでdegraded overrideを解除する。

## 17. 認証と安全性

- 既存Bearer token validationをshared Rust crateへ移植する。
- token比較はconstant-timeとする。
- token未設定かつ認証必須の場合はworkerを起動失敗させる。
- prompt、response、embedding原文、vectorを通常logへ出さない。
- model自動download、URL入力、remote code実行をproductionで禁止する。
- `trust_remote_code`相当を有効化しない。
- artifact directoryはowner writeのみ、config directoryは`0700`とする。
- tool定義はmodel入力として扱うだけで、shell・code・MCP実行機能をcompileしない。

## 18. Health、Status、Metrics

`/health`:

```json
{
  "ready": true,
  "service": "ornithd",
  "version": "0.1.0",
  "modelLoaded": true,
  "activeRequests": 0,
  "queueDepth": 0
}
```

`context-stilld local-model status --json`は少なくとも次を返す。

- desired state、observed state
- PID、process start time、restart count
- binary version、runtime commit、artifact revision
- configured profile、temporary profile、active profile
- context window、safe prompt、model max context
- MTP desired mode、effective mode、n_predict、degraded reason
- ready、queue depth、active request
- physical footprint、peak footprint
- last exit code、last error、next retry time

主要metrics:

```text
local_model_requests_total
local_model_errors_total
local_model_queue_depth
local_model_active_requests
local_model_request_seconds
local_model_ttft_seconds
local_model_decode_tokens_per_second
local_model_prompt_tokens_total
local_model_completion_tokens_total
local_model_process_physical_bytes
local_model_metal_allocated_bytes
local_model_model_load_seconds
local_model_restarts_total
local_model_forced_stops_total
local_model_mtp_draft_tokens_total
local_model_mtp_accepted_tokens_total
embedding_batch_size
embedding_encode_seconds
```

metrics labelへrequest ID、prompt、model path、tokenを入れない。

## 19. メモリと性能予算

`ps RSS`だけではmacOS unified memoryとshared regionを正しく比較できない。受け入れでは`footprint`、`vmmap -summary`、mistral.rs/Metal metrics、`memory_pressure`、swap deltaを同時記録する。

初期release gate:

| 項目 | 上限・条件 |
|---|---|
| `embeddingd` FP32 idle | physical footprint 800MiB以下 |
| `ornithd` standard idle | physical footprint 10GiB以下 |
| `ornithd` long idle | physical footprint 16GiB以下 |
| 両worker standard 30分 | 継続的なswap増加なし |
| 両worker idle 8時間 | 1時間目以降の増加5%以内 |
| Embedding warm start | 30秒以内 |
| Ornith warm-cache start | 90秒以内 |
| stop all | 35秒以内、両port close |

上限を満たさない場合、先にprofile cache設定、UQFF shard、ONNX providerを調整する。品質fixtureを通さずに量子化率を上げない。

## 20. 実装対象ファイル

`local-llm`:

```text
Cargo.toml
Cargo.lock
rust-toolchain.toml
crates/
  local-model-contract/
    src/auth.rs
    src/errors.rs
    src/health.rs
    src/priority.rs
    src/shutdown.rs
  ornithd/
    src/main.rs
    src/config.rs
    src/app.rs
    src/engine/mod.rs
    src/engine/mistralrs.rs
    src/routes/chat.rs
    src/routes/responses.rs
    src/routes/models.rs
    src/context_budget.rs
    src/provider_contract.rs
    src/streaming.rs
  embeddingd/
    src/main.rs
    src/config.rs
    src/app.rs
    src/engine.rs
    src/routes.rs
    src/prefix.rs
  local-model-test-support/
    src/fake_ornith.rs
    src/fixtures.rs
models/
  manifests/
    ornith-1.0-9b-uqff-q4k.json
    multilingual-e5-small-onnx-fp32.json
scripts/
  verify_model_artifacts.py
  smoke_rust_local_models.py
  measure_local_model_memory.sh
docs/
  ornith-embedding-rust-daemon-implementation-plan.md
  local-model-operations.md
```

`contextStill`:

```text
crates/context-stilld/src/domains/local_model_lifecycle/
  mod.rs
  routing.rs
  service.rs
  repository.rs
  config.rs
  state.rs
  process.rs
  reconcile.rs
  health.rs
  mtp.rs
  control.rs
crates/context-stilld/src/domains/cli/routing.rs
crates/context-stilld/src/domains/cli/service.rs
crates/context-stilld/src/domains/resident_runtime/service.rs
crates/context-stilld/src/domains/runtime_sidecars/service.rs
crates/context-stilld/src/domains/doctor/service.rs
crates/context-stilld/src/domains/mod.rs
crates/context-stilld/src/shared/process.rs
crates/context-stilld/src/lib.rs
```

既存`embedding_lifecycle`は`local_model_lifecycle`へ段階的に統合し、2つの独立supervisorを残さない。`ProcessSupervisor`には待機付き終了、process start time、executable path照合を追加し、`MockSupervisor`にも同じ観測値を実装する。

production sourceの1fileは800行未満を必須とする。CIでRust、TypeScript、Pythonの非生成sourceを検査し、800行以上をfailさせる。generated、vendor、lockfile、model manifestは対象外とする。

## 21. 実装フェーズ

### Phase 0: 現行契約と基準値の固定

実施:

- Python LLM/Embeddingの全endpointについてrequest/response golden fixtureを作る。
- 通常chat、SSE、tool choice、invalid tool、context超過を保存する。
- query/passage各20件のEmbedding vectorと384次元を保存する。
- 現在の起動時間、idle/peak footprint、速度、port ownershipを記録する。
- 現在のMTP既定がoffであることをbaselineへ記録する。

完了条件:

- Rust実装の差分を機械比較できる。
- fixtureにsecret、実prompt、個人情報を含めない。

### Phase 1: Cargo workspaceと共通契約

実施:

- workspace、toolchain、dependency policyを追加する。
- auth、error、health、priority queue、shutdownを共通crateへ実装する。
- fake engineで両HTTP serverのroute testを作る。
- line count、fmt、clippy、unit testをCIへ追加する。

完了条件:

- modelなしで全endpoint schemaをtestできる。
- `cargo fmt --check`、`cargo clippy --all-targets -- -D warnings`、`cargo test`が成功する。

### Phase 2: Embedding native化

実施:

- 固定revisionからFP32 ONNX artifactを取得しmanifestを作る。
- `UserDefinedEmbeddingModel`、mean pooling、normalizationを実装する。
- priority queue、batch制限、timeout、shutdownを実装する。
- Python版golden vectorと比較する。

完了条件:

- cosine similarity 0.999以上を全fixtureで満たす。
- top-10 retrievalの重複率100%を満たす。
- 1000連続request、同時8client、stop試験に成功する。

### Phase 3: Ornith artifactとMetal PoC

実施:

- source revisionを固定してQ4K UQFFを生成する。
- `UqffMultimodalModelBuilder`でM4からoffline loadする。
- MTP off、standard profile、greedy text生成を最初に通す。
- 32Kと128KのPagedAttention起動設定を実測する。
- warmup、cancel、process終了時のMetal解放を確認する。

完了条件:

- networkなしでloadと100 token生成が成功する。
- 32K/128Kの両profileでOOM、panic、Metal timeoutがない。
- process終了後にMetal allocationが残存しない。

### Phase 4: Ornith API parity

実施:

- models、chat completions、responsesを実装する。
- SSE、usage、finish reason、reasoning、tool callを変換する。
- context budgetと構造化errorを実装する。
- queue、timeout、disconnect cancel、graceful shutdownを実装する。

完了条件:

- Phase 0のprovider contract fixtureを全て通す。
- OpenAI Python/TypeScript SDKのsmokeが成功する。
- disconnect後にactive requestが0へ戻る。

### Phase 5: `context-stilld` lifecycleとProfile

実施:

- SQLite runtime settings、runtime state、Unix control socketを実装する。
- `local-model`のstart、stop、restart、status、profile、doctorを実装する。
- standard/long切替時にornithだけrestartする。
- stopped stateをreboot/reconcile越しに維持するtestを作る。

完了条件:

- CLI契約とexit codeがtestで固定される。
- `profile set long`中もEmbedding requestが成功する。
- `stop all`後に両processと両portが存在しない。

### Phase 6: MTP評価とauto mode

実施:

- off、n=2、n=3のbenchmark runnerを実装する。
- token parity、tool parity、速度、memoryを判定する。
- benchmark結果をhardware/artifact単位で保存する。
- crash-loop時のMTP off縮退を実装する。

完了条件:

- `auto`が判定結果どおりのeffective modeになる。
- 不合格時にMTP offで通常機能が全て使える。
- 3回crash testで`running_degraded`へ遷移する。

### Phase 7: Resident・配布・launchd統合

実施:

- resident reconcile loopへ`local_model_lifecycle`を接続する。
- 現在のEmbedding child管理を新domainへ移し、旧domainのspawn経路を削除する。
- worker binaryとartifact pathの解決をinstall/updateへ接続する。
- launchd ownershipを`context-stilld`だけにする。
- login起動、crash recovery、ログrotate、upgradeを検証する。

完了条件:

- login後、desired=runningのworkerだけがreadyになる。
- worker個別LaunchAgentが存在しない。
- supervisor restartで二重processを作らない。

### Phase 8: 切替とPython撤去

実施:

- Rust版を別portでshadow比較する。
- maintenance windowでPython版を停止してRust版を既存portへ切り替える。
- 24時間canary、7日間常用、再起動試験を行う。
- 合格後、production scriptとlaunchdからPython経路を削除する。
- virtualenvとmodelを即削除せず、明示的なcleanup手順を別にする。

完了条件:

- 常駐Python processが0。
- Python executableがworker起動経路に含まれない。
- 7日間、手動介入を要するcrashが0。
- rollback手順を一度実演済み。

## 22. テスト計画

### 22.1 Unit

- config migrationとatomic write
- auth有効/無効、token不一致
- error schema
- priority ordering、FIFO、queue full、timeout
- start/stopの冪等性
- desired/observed state transition
- crash backoffとcooldown
- standard/longのtoken budget
- context profile error
- query/passage prefix
- mean pooling、normalization、empty vector防止
- OpenAI schema変換
- tool arguments validation
- SSE event sequence
- MTP auto判定

### 22.2 ModelなしIntegration

- fake engineを使った全endpoint
- server bind失敗
- artifact missing/hash mismatch
- request body上限
- client disconnect
- graceful shutdown
- supervisor喪失時のworker扱い
- stale PIDとPID再利用
- portを別processが所有する場合の起動拒否

### 22.3 実model Integration

- Embedding FP32 parity 40 fixture
- 日本語、英語、code、長文chat
- 32K境界の直下・直上
- 128Kで100K以上の実prompt prefill
- tool callとtool result follow-up
- SSEの先頭、末尾、usage
- MTP off/on parity
- 100連続Ornith request
- 1000連続Embedding request
- Ornith推論中のEmbedding応答
- SIGTERM、SIGKILL fallback、再起動

### 22.4 長時間

- 両worker8時間idle
- standardで30分連続生成
- Embedding同時8clientを30分
- Ornith 1client + Embedding 8clientを30分
- 128K request完了後、idle grace経過で自動的にstandardへ戻ることとmemoryを確認
- supervisorを10回restartして重複processがないことを確認

## 23. CI

通常CIはmodel artifactをdownloadしない。

必須job:

- macOS arm64: build、unit、fake integration
- Linux: schema、state machine、HTTP contract
- `cargo fmt --check`
- `cargo clippy --workspace --all-targets -- -D warnings`
- `cargo test --workspace`
- `cargo audit`
- `cargo deny check`
- source file 800行未満check
- manifest schemaとhash list形式check

実model testはM4 self-hosted runnerまたは明示的なmanual workflowで実行する。artifact cache keyはmodel revision、manifest hash、runtime commitを含める。CI logへaccess token、prompt本文、signed URLを出さない。

## 24. 受け入れ基準

### 24.1 Lifecycle

- `start all`後に両workerがreadyになる。
- `stop all`後にprocess、port、Metal/ORT sessionが残らない。
- `desired=stopped`がloginとsupervisor restart後も維持される。
- crash時に当該workerだけが再起動する。
- profile変更でEmbeddingが停止しない。

### 24.2 API

- 現行主要endpointとauth contractに破壊的変更がない。
- Chat CompletionsとResponsesのstream/non-streamが動く。
- tool choice、tool arguments、finish reasonがfixture一致する。
- Embeddingは384次元、prefix、normalizeを維持する。
- context超過をsilent truncateしない。

### 24.3 品質・性能

- Embedding cosine 0.999以上、retrieval top-10重複率100%。
- Ornithのprovider contract corpusでschema failure 0。
- MTP auto有効時はoff比10%以上のdecode改善とtoken parityを満たす。
- standardとlongが各memory予算内で動く。
- 30分負荷で継続的なswap増加がない。

### 24.4 配布

- arm64 release binaryを署名・notarizeできる。
- modelは固定revisionとSHA-256で再現できる。
- runtime起動時にnetwork accessしない。
- production常駐経路にPythonがない。
- worker個別LaunchAgentがない。

## 25. RolloutとRollback

切替順:

1. `embeddingd`を別portでshadow比較する。
2. EmbeddingだけRustへ切り替えて24時間観測する。
3. `ornithd`を別portでprovider contract比較する。
4. OrnithをMTP off、standardで切り替える。
5. 24時間後にlong profileを解放する。
6. benchmark合格後にMTP autoを解放する。
7. 7日安定後にPython起動経路を削除する。

Rollbackは移行期間限定の`CONTEXT_STILL_LOCAL_MODEL_BACKEND=legacy-python`を設定し、Rust workerを完全停止してからPython版を既存portで起動する。両方を同時bindしない。rollback時もmodel artifactとRust binaryは削除しない。

Python fallbackは移行期間だけの安全装置であり、最終リリースの運用仕様ではない。2回の安定release後にfallback code自体を削除する。

## 26. リスクと対策

| リスク | 対策 |
|---|---|
| MLX 4bitをmistral.rsで読めない | 元modelからUQFF Q4Kを生成し、manifest固定 |
| Qwen3.5 hybridのMetal不具合 | v0.9.2 commit固定、M4実model PoCをAPI実装前のgateにする |
| MTPが新しく不安定 | offで全機能成立、autoはbenchmark合格必須、crash時縮退 |
| 128Kでmemory不足 | 起動profile分離、16GiB上限、silent fallback禁止 |
| ONNXとsentence-transformersの差 | FP32、mean pooling、L2 normalizeでgolden比較 |
| CoreMLでoperator差・memory増 | 初期版CPU、CoreMLは後段比較でのみ採用 |
| supervisor二重化 | launchd ownershipをcontext-stilldだけに限定 |
| stop後に再起動される | signalより先にdesired=stoppedをatomic保存 |
| PID再利用で別processを停止 | PID start time、binary path、port ownershipを照合 |
| Rust SDK API変更 | git commitとCargo.lock固定、upgradeは別PR |
| 1file肥大化 | module責務を固定し、CIで800行未満を強制 |
| Python fallbackが恒久化 | Phase 8完了条件と削除releaseを明記 |

## 27. 実装着手順

実装担当は次の順を崩さない。

1. Phase 0 fixtureを先にmergeする。
2. Phase 1のfake serverと契約testをmergeする。
3. `embeddingd`を完成させる。
4. Ornith UQFFのM4 PoCを独立PRで通す。
5. `ornithd` APIを実装する。
6. lifecycle/profileを`context-stilld`へ実装する。
7. MTPは最後に追加する。
8. Rust版の受け入れ完了前にPython起動経路を削除しない。

各PRは1phaseまたは1phase内の明確な縦sliceに限定する。API schema変更、model artifact変更、runtime dependency更新を同じPRへ混在させない。

## 28. Definition of Done

次を全て満たした時点で、この計画を完了とする。

- Phase 0から8の完了条件を満たす。
- 全CIとM4実model workflowが成功する。
- standard/long、MTP off/auto、start/stop/restartを実機で確認する。
- `../contextStill`から両workerを管理できる。
- 現行利用クライアントの設定変更が不要である。
- 常駐process一覧にLLM/Embedding用途のPythonが存在しない。
- `local-model-operations.md`へinstall、status、profile切替、stop、rollbackを記載する。
- security、memory、failure recoveryのreview指摘が0になる。
- production sourceに800行以上のfileがない。

## 29. 参照

- [Rust 1.98.0 release](https://blog.rust-lang.org/2026/08/20/Rust-1.98.0/)
- [mistral.rs v0.9.2 release](https://github.com/EricLBuehler/mistral.rs/releases/tag/v0.9.2)
- [mistral.rs Qwen3.5 MTP実装 #2372](https://github.com/EricLBuehler/mistral.rs/pull/2372)
- [mistral.rs Qwen3.5 hybrid model修正 #2373](https://github.com/EricLBuehler/mistral.rs/pull/2373)
- [mistral.rs MTP性能改善 #2385](https://github.com/EricLBuehler/mistral.rs/pull/2385)
- [mistral.rs UQFF format](https://github.com/EricLBuehler/mistral.rs/blob/v0.9.2/docs/src/content/docs/reference/uqff-format.md)
- [Ornith-1.0-9B](https://huggingface.co/ornith-ai/Ornith-1.0-9B)
- [Ornith config.json](https://huggingface.co/ornith-ai/Ornith-1.0-9B/blob/main/config.json)
- [MLX Ornith-1.0-9B-4bit](https://huggingface.co/mlx-community/Ornith-1.0-9B-4bit)
- [fastembed-rs v5.17.4](https://github.com/Anush008/fastembed-rs/releases/tag/v5.17.4)
- [intfloat/multilingual-e5-small ONNX](https://huggingface.co/intfloat/multilingual-e5-small/tree/main/onnx)
