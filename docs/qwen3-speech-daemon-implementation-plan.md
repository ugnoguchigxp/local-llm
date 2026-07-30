# Qwen3 Speech Daemon 実装計画書

作成日: 2026-07-30

対象環境: Apple Silicon Mac（M4、32GBユニファイドメモリ）

状態: 初期リリース実装・実機受け入れ完了

## 0. 実装結果

2026年7月30日に、この文書の初期リリース範囲を実装し、M4・32GB Macで実モデル受け入れを完了した。

- TTS: `127.0.0.1:44520`、Qwen3-TTS 1.7B CustomVoice BF16
- ASR: `127.0.0.1:44521`、Qwen3-ASR 1.7B 8bit
- 常駐: `com.localLlm.qwen3-tts`、`com.localLlm.qwen3-asr`
- TTS: REST/chunked PCM・WAV・MP3・Opus、buffered AAC・FLAC
- TTS拡張: 自然言語instructions、3 preset、構造化style、speed、seed、CustomVoice、Base clone、VoiceDesign
- ASR: REST、SSE、WebSocket binary PCM/JSON Base64、VAD、word timestamp
- OpenAI互換: OpenAI Python SDK 2.50.0からTTS streamingとASR RESTを実呼び出し済み
- モデル: 固定revision 7件、合計24,669,367,943 bytes、全ファイルSHA-256記録済み
- 実モデルsmoke: TTS→ASR REST→SSE→WebSocket→ForcedAlignerを完走
- TTS連続試験: 10/10成功、温間TTFA平均0.252秒、最大0.358秒
- TTS長文比較: BF16 RTF 2.19、6bit RTF 2.28。同一M4では6bitが高速化しなかったためBF16を採用
- ASR比較: 6.32秒音声で8bit平均0.93秒、BF16平均4.73秒、認識文は一致。8bitを採用
- 切断試験: TTSを1秒で切断後、推論gateが即時`active=0`へ復帰

運用コマンド:

```bash
./scripts/speech_launchd status all
./scripts/speech_launchd restart all
speech/.venv-asr/bin/python scripts/smoke_speech_api.py
```

## 1. 結論

Qwen3ベースの音声基盤を、次の2デーモンだけで構築する。

1. `qwen3-tts-daemon`
   - RESTによる音声生成
   - HTTP chunked responseによる音声ストリーミング
   - CustomVoice、VoiceDesign、音声クローン、音声プロファイル管理
2. `qwen3-asr-daemon`
   - RESTによる音声ファイル文字起こし
   - SSEによるファイル文字起こしの逐次結果
   - WebSocketによるマイク／PCMリアルタイム認識
   - 単語・セグメントタイムスタンプ

新しいGateway、Redis、モデル別ワーカー、ForcedAligner専用プロセスは作らない。既存リポジトリに導入済みのFastAPI、Uvicorn、認証、lifespan、OpenAI互換APIの実装パターンを再利用する。

推論ランタイムはMLXとし、初期実装ではRustを採用しない。推論処理の主体はMetalであり、HTTP層だけをRustへ置き換えても初期段階では効果が限定的なためである。API契約とモデルアダプターを分離し、実測でHTTP／ストリーム処理がボトルネックになった場合だけRustへ置換できる構造にする。

## 2. 目的

次の状態を初期リリースの到達点とする。

- Qwen3-TTS 1.7BとQwen3-ASR 1.7BをApple Silicon上でMetal実行する。
- TTSとASRを独立したlaunchdデーモンとして常駐させる。
- OpenAI Audio APIに近いREST APIを提供する。
- TTSは生成済み音声のREST取得と、生成途中から再生できるストリーミングの両方を提供する。
- ASRはファイル単位REST、SSE、リアルタイムWebSocketの3経路を提供する。
- TTSは感情、口調、速度、抑揚、声質、音声デザイン、許可済み音声のクローンを扱える。
- モデルは起動時にダウンロードせず、固定revisionをローカルからオフラインロードする。
- 32GB環境で既存LLMデーモンとの共存可否を計測し、メモリ逼迫時にオンデマンドモデルを解放する。

## 3. 非目標

- 0.6Bモデルを速度だけを理由に標準モデルへしない。
- PyTorch CPU実行を標準経路にしない。
- vLLM、CUDA、ROCmをMac版へ導入しない。
- TTS、ASR、ForcedAlignerを別々のプロセスへ分割しない。
- 初期段階でWebRTC、SIP、ブラウザ向けAECを実装しない。
- ASRモデル単体で翻訳APIを実装したように見せない。
- 音声クローンを同意確認なしで利用できるAPIにしない。
- 既存のLLM用FastAPIアプリへ音声モデルを直接ロードしない。

## 4. 既存基盤

このリポジトリには、既に次の基盤が存在する。

- `requirements.txt`
  - `fastapi>=0.115.0`
  - `uvicorn>=0.32.0`
- `api/main.py`
  - FastAPI lifespan
  - モデルpreload
  - graceful shutdown
  - 認証付きrouter
  - health/status endpoint
- `api/routes/chat.py`
  - REST
  - `StreamingResponse`を使ったSSE
  - daemon busy時のエラー処理
- `scripts/run_openai_api.sh`
  - Uvicorn起動
- `launchd/`
  - LLM、Embeddingデーモンの常駐パターン

音声デーモンはこれらのパターンを再利用する。ただしモデル依存関係を分離するため、TTSとASRはそれぞれ専用Python 3.12環境を使用する。

## 5. 全体構成

```text
OpenAI SDK / curl / Browser / Native Client
          │
          ├─────────────────────────────────────┐
          │                                     │
          ▼                                     ▼
qwen3-tts-daemon                         qwen3-asr-daemon
127.0.0.1:44520                         127.0.0.1:44521
FastAPI + Uvicorn                       FastAPI + Uvicorn
          │                                     │
          ├─ CustomVoice 1.7B 常駐               ├─ ASR 1.7B 常駐
          ├─ Base 1.7B オンデマンド              ├─ Streaming Session
          └─ VoiceDesign 1.7B オンデマンド       └─ ForcedAligner オンデマンド
```

TTSのBaseとVoiceDesignは「副モデルスロット」を共有し、同時には1モデルだけをロードする。ASRのForcedAlignerもASRデーモン内で必要時だけロードする。

## 6. 技術選定

### 6.1 TTS

- ランタイム: `mlx-audio`
- 実装固定: `mlx-audio @ ada4c2670b0fa4be9d5115a1d4a629e61d183fd6`
- HTTP: 既存FastAPI/Uvicorn
- 出力変換: `ffmpeg` subprocessまたは対応ライブラリ

`mlx-audio`のQwen3-TTS実装は、CustomVoice、VoiceDesign、参照音声によるクローン、音声チャンクの逐次生成を提供している。

### 6.2 ASR

- ランタイム: `mlx-qwen3-asr`
- 初期固定: `mlx-qwen3-asr==0.3.5`
- ソース確認基準: `f069a0f2158b401c205c4d68633d3e3f3c5af469`
- HTTP/WebSocket: 既存FastAPI/Uvicorn
- 音声変換: WAV fast path、その他は`ffmpeg`
- VAD: ASRデーモン内の軽量VAD

組み込みHTTPサーバーをそのまま別プロセスで起動せず、`Session`とstreaming APIをFastAPIアプリ内から利用する。これにより、既存認証、キュー制御、メトリクス、REST/SSE/WebSocket契約を同じ実装方針へ揃える。

### 6.3 Python環境

```text
speech/.venv-tts
speech/.venv-asr
```

両環境でFastAPI/Uvicornを使用する。これは新規Web基盤の採用ではなく、既存基盤を依存関係の異なる2デーモンで安全に動かすための分離である。

## 7. モデル構成

### 7.1 TTS

| 用途 | モデル | revision | 常駐 |
|---|---|---|---|
| 標準音声生成 | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16` | `52f4770fd9726457eae3d3b6aa92047a25a10776` | 常駐 |
| 性能比較用 | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit` | `1c6c0ff58c43afa8df571facde2efa077efd85e2` | 非常駐 |
| 音声クローン | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16` | `a6eb4f68e4b056f1215157bb696209bc82a6db48` | オンデマンド |
| 音声設計 | `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16` | `7d3824abff87e49756bb0f83fb5411de75d160c4` | オンデマンド |

BF16と6bitを同一テキスト、seed、streaming intervalで比較した結果、M4ではBF16の方がわずかに高速だった。標準は品質優先のBF16とし、6bitはメモリ制約環境向けの比較候補として保持する。

### 7.2 ASR

| 用途 | モデル | revision | 常駐 |
|---|---|---|---|
| 文字起こし | `mlx-community/Qwen3-ASR-1.7B-8bit` | `a8379a2e2f9e313c9292cdf1af4055ab56d50d55` | 常駐 |
| 品質比較用 | `mlx-community/Qwen3-ASR-1.7B-bf16` | `e1f6c266914abc5a46e8756e02580f834a6cf8a7` | 非常駐 |
| タイムスタンプ | `Qwen/Qwen3-ForcedAligner-0.6B` | `c7cbfc2048c462b0d63a45797104fc9db3ad62b7` | オンデマンド |

同一音声の実機比較で認識文が一致し、8bitが約5.1倍高速だったため8bitを標準採用した。BF16は品質差を追加評価するための参照モデルとして保持する。0.6B ASRは初期評価対象に含めない。

## 8. モデルダウンロード

### 8.1 配置場所

```text
~/Library/Application Support/local-llm/speech/
  models/
    tts/
    asr/
    aligner/
  voices/
  cache/
  tmp/
  models.manifest.json
```

ディレクトリのパーミッションはユーザーのみ読み書き可能にする。参照音声、一時音声、音声プロファイルを通常ログへ出力しない。

### 8.2 ダウンロード方針

- `main`や未固定tagからロードしない。
- `huggingface_hub.snapshot_download()`へrevision SHAとlocal directoryを必ず渡す。
- ダウンロード処理とデーモン起動処理を分離する。
- ダウンロード完了後にファイル一覧、合計サイズ、revision、主要ファイルSHA-256をmanifestへ保存する。
- ASR量子化物には、元モデルrevision、変換コードrevision、bit数、group size、生成ファイルSHA-256を保存する。
- デーモン起動時は`HF_HUB_OFFLINE=1`と`TRANSFORMERS_OFFLINE=1`を設定する。
- 起動時にモデルが不足している場合はダウンロードせず、`/ready`を503にして明確なエラーを返す。

### 8.3 ディスク予算

TTS 3モデル、ASR原本、ASR BF16/8bit変換、ForcedAligner、1世代のロールバックを考慮し、音声基盤用に40GBを確保する。

## 9. デーモン単位

### 9.1 TTS

```text
label: com.localLlm.qwen3-tts
host: 127.0.0.1
port: 44520
working directory: repository root
python: speech/.venv-tts/bin/python
app: speech.tts.app:app
```

起動時:

1. 設定とモデルmanifestを検証する。
2. CustomVoice 1.7Bをロードする。
3. 固定短文でウォームアップする。
4. 音声チャンクを1個以上生成できることを確認する。
5. Ready状態へ移行する。

### 9.2 ASR

```text
label: com.localLlm.qwen3-asr
host: 127.0.0.1
port: 44521
working directory: repository root
python: speech/.venv-asr/bin/python
app: speech.asr.app:app
```

起動時:

1. 設定とモデルmanifestを検証する。
2. 選定済みASR 1.7Bを`Session`としてロードする。
3. 固定長の無音PCMでMetal実行経路をウォームアップする。
4. 実モデルsmokeで固定日本語音声の認識を確認する。
5. Ready状態へ移行する。

### 9.3 launchd

両デーモンに次を適用する。

- `RunAtLoad=true`
- 異常終了時だけ再起動
- `ThrottleInterval=5`
- `SIGTERM`でgraceful shutdown
- 停止猶予30秒
- stdout/stderrを`.debug/`配下の別ファイルへ出力
- `127.0.0.1`以外へbindしない
- モデルロード後にforkしない

連続5回の起動失敗またはメモリ不足を検出した場合は無限再起動を避け、Ready失敗理由をログへ残す。

## 10. 共通API規約

### 10.1 認証

既存のBearer認証パターンを再利用する。

```http
Authorization: Bearer <local-api-key>
```

`/live`だけは認証なし、その他は原則認証ありとする。音声クローン／音声登録endpointには管理用の追加scopeを要求できる設計にする。

### 10.2 エラー形式

OpenAI互換のエラー形へ統一する。

```json
{
  "error": {
    "message": "model is not ready",
    "type": "service_unavailable_error",
    "param": null,
    "code": "model_not_ready"
  }
}
```

主なHTTP status:

- `400`: 入力形式、音声形式、パラメータ不正
- `401`: 認証失敗
- `404`: モデル、音声プロファイル不明
- `409`: 音声プロファイル更新競合
- `413`: 入力音声またはテキスト上限超過
- `422`: スキーマ検証失敗
- `429`: キュー満杯
- `499`相当の内部記録: クライアント切断
- `503`: モデル未Ready、メモリ不足、停止中
- `504`: 推論タイムアウト

### 10.3 共通endpoint

両デーモンが提供する。

```http
GET /live
GET /ready
GET /health
GET /metrics
GET /v1/models
```

`/ready`は次をすべて満たす場合だけ200を返す。

- 標準モデルがロード済み
- ウォームアップ成功
- 推論キューが受付可能
- メモリpressureが許容範囲
- shutdown開始前

## 11. TTS REST API

### 11.1 音声生成

```http
POST /v1/audio/speech
Content-Type: application/json
```

標準入力:

```json
{
  "model": "qwen3-tts-1.7b-custom-voice",
  "input": "読み上げる文章です。",
  "voice": "ono_anna",
  "instructions": "落ち着いた口調で、少しゆっくり話してください。",
  "response_format": "wav",
  "speed": 0.95
}
```

標準フィールド:

| フィールド | 必須 | 内容 |
|---|---:|---|
| `model` | yes | 公開モデルalias |
| `input` | yes | 読み上げテキスト |
| `voice` | yes | プリセット名または`voice_*` ID |
| `instructions` | no | 自然言語の話し方指定 |
| `response_format` | no | `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` |
| `speed` | no | `0.25`～`4.0`、標準`1.0` |

Qwen拡張は`qwen`オブジェクトへ隔離し、OpenAI SDKでは`extra_body`から渡せるようにする。

```json
{
  "qwen": {
    "language": "Japanese",
    "mode": "custom_voice",
    "style_preset": "calm_narration",
    "style": {
      "emotion": "gentle",
      "pace": "slightly_slow",
      "pitch": "slightly_low",
      "energy": "soft",
      "accent": "standard_japanese",
      "intonation": "natural",
      "pause": "clear",
      "whisper": false
    },
    "seed": 1234
  }
}
```

`instructions`、`speed`、`style_preset`、構造化`style`は、順序を固定して1つのQwen `instruct`へ変換する。再現性試験ができるよう、変換済み指示のハッシュをデバッグメタデータへ残す。本文や参照音声は残さない。

### 11.2 非ストリーミング応答

通常クライアントがresponse bodyを最後まで取得した場合は、完成音声として利用できる。

主なresponse header:

```text
Content-Type
Content-Length
X-Request-Id
X-Model-Id
X-Model-Revision
X-Audio-Sample-Rate
X-Audio-Channels
X-Audio-Encoding
X-Processing-Time-Ms
X-Real-Time-Factor
```

### 11.3 対応形式

- 非ストリーミング: `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm`
- ストリーミング保証: `pcm`, `wav`, `mp3`, `opus`
- `aac`, `flac`のストリーミングは初期受け入れ対象外とし、指定時はbuffered responseへ切り替える。

## 12. TTS Streaming API

TTSはRESTと別のWebSocket endpointを増やさず、同じ`POST /v1/audio/speech`からHTTP chunked responseを返す。OpenAI SDKのstreaming response利用方法と一致させる。

サーバー実装:

- MLX生成器が返す音声チャンクを`StreamingResponse`へ逐次渡す。
- PCMは生成チャンクをそのままlittle-endian PCM16へ変換する。
- WAVはストリーミング可能なヘッダーを先頭へ出力する。
- MP3/Opusは常駐`ffmpeg`プロセスではなく、リクエスト単位のpipeへ流す。
- 最初の有効音声が生成されるまで空チャンクを送らない。
- クライアント切断時は生成iteratorとencoderを停止する。
- 最終チャンク後にencoderをflushし、欠落した文末がないことを確認する。

ストリーム利用側がbodyを逐次読めばリアルタイム再生でき、通常のRESTクライアントが最後まで読めば完成音声になる。独自の`stream`必須フィールドには依存しない。

初期の推奨形式:

```json
{
  "response_format": "pcm"
}
```

PCM responseでは次のheaderを必須とする。

```text
X-Audio-Sample-Rate
X-Audio-Channels
X-Audio-Encoding: pcm_s16le
```

## 13. TTS音声管理API

### 13.1 同意音声

```http
POST /v1/audio/voice_consents
Content-Type: multipart/form-data
```

入力:

- `name`
- `language`
- `recording`
- `owner`
- `usage_scope`

### 13.2 音声プロファイル作成

```http
POST /v1/audio/voices
Content-Type: multipart/form-data
```

入力:

- `name`
- `audio_sample`
- `reference_text`
- `consent`
- `language`

成功時は`voice_*` IDを返し、通常の`/v1/audio/speech`の`voice`へ指定できる。

### 13.3 VoiceDesign

```http
POST /v1/audio/voices/design
Content-Type: application/json
```

VoiceDesignは管理操作とし、生成音声へ`source=voice_design`と説明metadataを付けて保存する。

処理:

1. VoiceDesignモデルを副モデルスロットへロードする。
2. 指定された声質説明から複数候補を生成する。
3. 候補にIDを付けてローカル保存する。
4. 生成したvoice IDをBase用参照音声として利用可能にする。
5. VoiceDesignモデルをアイドル10分後に解放する。

その他:

```http
GET /v1/audio/voices
GET /v1/audio/voices/{voice_id}
DELETE /v1/audio/voices/{voice_id}
```

削除は音声ファイルを即時破棄せず、最初にtrash領域へ移す回復可能な処理にする。

## 14. ASR REST API

### 14.1 同期文字起こし

```http
POST /v1/audio/transcriptions
Content-Type: multipart/form-data
```

入力:

| フィールド | 必須 | 内容 |
|---|---:|---|
| `file` | yes | 音声または動画 |
| `model` | yes | `qwen3-asr-1.7b` |
| `language` | no | 日本語既知なら`ja`または`Japanese` |
| `prompt` | no | 固有名詞、技術用語、文脈 |
| `response_format` | no | `json`, `text`, `verbose_json`, `srt`, `vtt` |
| `temperature` | no | 初期実装では`0`のみ |
| `timestamp_granularities[]` | no | `segment`, `word` |
| `stream` | no | `false`で同期、`true`でSSE |

内部音声形式:

```yaml
sample_rate: 16000
channels: 1
encoding: float32
```

入力上限:

- request body: 512MB
- 音声長: 2時間
- 同期REST推奨: 20分以内
- 20分を超える入力: async job APIまたはクライアント分割を案内

初期実装では長時間ジョブ用の別デーモンを作らない。必要ならASRデーモン内に次のローカルjob APIを追加する。

```http
POST /v1/audio/transcription_jobs
GET /v1/audio/transcription_jobs/{job_id}
DELETE /v1/audio/transcription_jobs/{job_id}
```

### 14.2 タイムスタンプ

`timestamp_granularities[]`が指定された場合だけForcedAlignerをロードする。

- `segment`: 字幕向けフレーズ区間
- `word`: 単語または日本語token単位
- 部分認識中の時刻は確定値として保存しない。
- 確定transcriptに対してalignerを実行し、最終結果だけ時刻を返す。
- 5分を超える区間はVAD境界で分割し、元音声offsetを加算する。

### 14.3 非対応endpoint

```http
POST /v1/audio/translations
```

Qwen3-ASR単体は翻訳モデルではないため、初期版ではOpenAI形式の`501 Not Implemented`を返す。

## 15. ASR SSE

`POST /v1/audio/transcriptions`に`stream=true`を指定した場合、アップロード済み音声をチャンク処理し、SSEで進捗と部分結果を返す。

イベント:

```text
transcript.started
transcript.delta
transcript.segment
transcript.completed
error
```

例:

```text
event: transcript.delta
data: {"request_id":"asr_...","sequence":4,"delta":"音声認識の","text":"この音声認識の"}
```

規約:

- `sequence`は1から単調増加する。
- `delta`は追加候補、`text`は現在の発話全体を表す。
- クライアントは`text`で置換できるようにする。
- `transcript.completed`だけを確定結果として保存する。
- timestampは`completed`で返し、partialには保証しない。
- 最後に`data: [DONE]`を返す。
- 切断時は残り処理をキャンセルする。

## 16. ASR WebSocket

### 16.1 endpoint

```http
WebSocket /v1/audio/transcriptions/stream
```

接続時にBearer tokenを検証する。ブラウザでAuthorization headerを設定できない場合に備え、短命なsession tokenをRESTで発行できる設計にする。恒久API keyをquery stringへ入れない。

### 16.2 入力

標準入力は次とする。

```yaml
sample_rate: 16000
channels: 1
encoding: pcm_s16le
frame_ms: 20-100
```

効率を優先するクライアントはbinary frameでPCMを送る。OpenAI Realtime形式へ寄せるクライアント向けに、JSON＋Base64の`input_audio_buffer.append`も受け入れられるようにする。

クライアントイベント:

```text
transcription_session.update
input_audio_buffer.append
input_audio_buffer.commit
input_audio_buffer.clear
session.close
```

設定例:

```json
{
  "type": "transcription_session.update",
  "session": {
    "language": "Japanese",
    "prompt": "NightWorkers ContextStill Qwen3 MLX",
    "input_audio_format": "pcm16",
    "sample_rate": 16000,
    "vad": {
      "enabled": true,
      "speech_start_ms": 150,
      "speech_end_ms": 650,
      "pre_roll_ms": 250,
      "max_utterance_seconds": 30
    }
  }
}
```

### 16.3 出力

サーバーイベント:

```text
transcription_session.created
transcription_session.updated
input_audio_buffer.speech_started
input_audio_buffer.speech_stopped
conversation.item.input_audio_transcription.delta
conversation.item.input_audio_transcription.completed
session.completed
error
```

部分結果:

```json
{
  "type": "conversation.item.input_audio_transcription.delta",
  "item_id": "item_...",
  "sequence": 5,
  "delta": "基盤を",
  "text": "音声基盤を"
}
```

確定結果:

```json
{
  "type": "conversation.item.input_audio_transcription.completed",
  "item_id": "item_...",
  "language": "Japanese",
  "transcript": "音声基盤を構築します。",
  "duration_ms": 2840,
  "processing_ms": 610
}
```

### 16.4 VADと確定処理

初期値:

```yaml
frame_ms: 20
speech_start_confirmation_ms: 150
speech_end_silence_ms: 650
pre_roll_ms: 250
max_utterance_seconds: 30
```

処理:

1. pre-roll ring bufferへPCMを保持する。
2. VADが発話開始を検知する。
3. streaming stateへ音声を供給する。
4. 更新可能なpartialを返す。
5. 発話終了またはcommitを検知する。
6. 同じ1.7Bモデルでtail refinementを実行する。
7. final transcriptを返す。
8. timestamp要求がある場合は発話確定後にalignerを実行する。

### 16.5 バックプレッシャー

- 1sessionあたり未処理音声30秒まで。
- 送信速度が処理速度を継続的に超えた場合はwarning eventを返す。
- 60秒を超えた場合は`audio_buffer_overflow`でcloseする。
- ASRデーモン全体の初期最大WebSocket session数は1。
- close codeは通常終了1000、認証1008、過負荷1013、内部異常1011を使う。

## 17. キューと同時実行

初期値:

```yaml
tts:
  inference_concurrency: 1
  queue_size: 8
  timeout_short_seconds: 60
  timeout_long_seconds: 300

asr:
  inference_concurrency: 1
  queue_size: 8
  websocket_sessions: 1
  request_timeout_seconds: 300
  stream_idle_timeout_seconds: 30
  stream_max_session_seconds: 3600
```

TTSとASRは別プロセスなので相互にはブロックしない。ただしMetal GPUは共有されるため、同時推論試験で単独時の2倍を超える遅延が出た場合は、2デーモン間で協調するadvisory lockを追加する。このlockは独立サービスにせず、TTSを高優先度、ASRを音声チャンク境界で譲歩可能とする。

## 18. モデル常駐とメモリ

### 18.1 通常時

- TTS CustomVoice 1.7B: 常駐
- ASR 1.7B BF16または8bit: 常駐
- TTS Base: 未ロード
- TTS VoiceDesign: 未ロード
- ForcedAligner: 未ロード

### 18.2 オンデマンド

- BaseとVoiceDesignは同じ副モデルスロットを共有する。
- スロット切り替え前に実行中ジョブがないことを確認する。
- 切り替え後にMLX cacheを解放する。
- アイドル10分で副モデルを解放する。
- ForcedAlignerはtimestampリクエスト単位でロードし、処理完了後に解放する。

### 18.3 受け入れ予算

- 通常常駐時の音声2デーモン合計物理メモリ: 16GB以内
- オンデマンドモデル利用時のピーク: 20GB以内
- 通常試験30分で継続的なswap増加がない
- macOS `memory_pressure`が継続してcriticalにならない
- 既存LLMデーモンをidle常駐させた共存試験を別途実施する

## 19. Graceful Shutdownとキャンセル

SIGTERM受信時:

1. Readyをfalseにする。
2. 新規REST／WebSocket受付を停止する。
3. SSEとWebSocketへ終了イベントを送る。
4. 実行中ジョブを最大30秒待つ。
5. encoder subprocessを終了する。
6. 一時ファイルを削除する。
7. 音声プロファイルmetadataをflushする。
8. MLXモデルを解放する。
9. プロセスを終了する。

クライアント切断時:

- TTS生成iteratorを閉じる。
- ASRの未確定session stateを破棄する。
- ForcedAlignerを新規起動しない。
- 一時ファイルを削除する。
- 切断を通常のサーバーエラーとして数えない。

## 20. セキュリティとプライバシー

- `127.0.0.1`限定。
- 外部公開は別途明示的なreverse proxy設定がある場合だけ許可する。
- 音声本文、参照音声、transcriptを通常ログへ書かない。
- URLから参照音声を取得する機能は初期版で無効にする。
- 音声クローンはconsent IDを必須にする。
- consent、参照音声、生成voiceの所有関係をmetadataへ保存する。
- 一時ディレクトリは`0700`。
- ファイル名へ利用者名や元ファイル名を埋め込まない。
- AI生成音声であることを利用側が表示できるmetadataを返す。
- WebSocket session tokenは短命かつ一度限りとする。

## 21. メトリクス

両デーモン共通:

```text
speech_requests_total
speech_errors_total
speech_queue_depth
speech_active_requests
speech_process_rss_bytes
speech_mlx_active_memory_bytes
speech_mlx_peak_memory_bytes
speech_model_load_seconds
speech_daemon_restarts_total
```

TTS:

```text
tts_time_to_first_audio_seconds
tts_generation_seconds
tts_audio_duration_seconds
tts_real_time_factor
tts_stream_cancellations_total
tts_model_switches_total
```

ASR:

```text
asr_processing_seconds
asr_audio_duration_seconds
asr_real_time_factor
asr_finalization_seconds
asr_partial_revisions_total
asr_dropped_audio_frames_total
asr_websocket_sessions
asr_aligner_seconds
```

request ID、モデルalias、response format、成功／失敗は記録してよい。本文、音声、音声プロファイルIDは通常メトリクスへ入れない。

## 22. 実装対象ファイル

予定構成:

```text
speech/
  __init__.py
  common/
    __init__.py
    errors.py
    metrics.py
    model_manifest.py
    settings.py
  tts/
    __init__.py
    app.py
    schemas.py
    service.py
    model_manager.py
    audio_encoder.py
    instruct.py
    voices.py
  asr/
    __init__.py
    app.py
    schemas.py
    service.py
    model_manager.py
    audio.py
    streaming.py
    vad.py
  tests/
    test_tts_routes.py
    test_tts_streaming.py
    test_tts_instruct.py
    test_voice_routes.py
    test_asr_routes.py
    test_asr_sse.py
    test_asr_websocket.py
    test_model_manifest.py
  requirements-tts.in
  requirements-tts.lock
  requirements-asr.in
  requirements-asr.lock

scripts/
  setup_speech_envs.sh
  download_speech_models.py
  convert_asr_model.py
  run_qwen3_tts_daemon.sh
  run_qwen3_asr_daemon.sh
  smoke_speech_api.py

launchd/
  com.localLlm.qwen3-tts.plist
  com.localLlm.qwen3-asr.plist
```

既存`api/main.py`へ音声routerを追加しない。既存LLM APIとの結合を避け、停止、再起動、依存関係、メモリ障害を分離する。

## 23. 実装フェーズ

### Phase 0: ベースライン固定

実施:

- 現在のLLM API unit test結果を保存する。
- 現在のFastAPI、Uvicorn、MLXバージョンを記録する。
- 既存launchdジョブの状態を記録する。
- M4、32GB、空きディスク、macOSバージョンをmanifestへ記録する。

完了条件:

- 音声実装前後の回帰比較ができる。
- 既存のユーザー変更へ不要な編集を加えていない。

### Phase 1: API契約とfake modelテスト

実施:

- TTS REST、chunked stream、voice APIのschemaを作成する。
- ASR REST、SSE、WebSocketのschemaとeventを作成する。
- OpenAI形式のerror responseを共通化する。
- fake TTS/ASR generatorを使うroute testを先に作成する。

完了条件:

- 実モデルなしで全endpointとstream event shapeをテストできる。
- RESTとStreamの契約変更をテスト差分としてレビューできる。

### Phase 2: モデル取得とオフラインロード

実施:

- 固定revision downloaderを実装する。
- TTS 3モデルを取得する。
- ASR 1.7BとForcedAlignerを取得する。
- ASR BF16/8bitを同じsource revisionから変換する。
- manifestとSHA-256を生成する。
- ネットワークを切った状態でロード試験を行う。

完了条件:

- 起動時のHugging Faceアクセスが0件。
- 不足モデル時に自動ダウンロードせずReady 503となる。

### Phase 3: TTS REST

実施:

- CustomVoiceを常駐ロードする。
- OpenAI互換requestをQwen3-TTS引数へ変換する。
- `instructions`と構造化styleを統合する。
- 全buffered response formatを実装する。
- request validation、キュー、timeout、キャンセルを実装する。

完了条件:

- 日本語、英数字混在、長文、感情指定がRESTで生成できる。
- OpenAI Python/TypeScript SDKから呼び出せる。

### Phase 4: TTS Streaming

実施:

- MLX audio chunkを`StreamingResponse`へ接続する。
- PCM/WAVを先に実装する。
- MP3/Opusのffmpeg pipeを実装する。
- disconnect、encoder flush、末尾欠落をテストする。

完了条件:

- 完成前に最初の有効音声を受信できる。
- 同じendpointを通常RESTクライアントでも利用できる。
- 10回連続生成で先頭／末尾欠落がない。

### Phase 5: ASR RESTとSSE

実施:

- ASR 1.7B Sessionを常駐させる。
- multipart file入力と音声正規化を実装する。
- JSON/text/verbose_json/SRT/VTTを実装する。
- `stream=true`のSSEを実装する。
- context／promptをQwen3-ASRへ渡す。
- timestamp要求時のForcedAlignerを実装する。

完了条件:

- 同一音声についてREST finalとSSE completedの本文が一致する。
- 日本語のword/segment timestampが単調増加する。

### Phase 6: ASR WebSocket

実施:

- session設定、binary PCM、Base64 appendを実装する。
- VAD、pre-roll、発話開始／終了eventを実装する。
- partial、tail refinement、finalを実装する。
- backpressure、idle timeout、切断処理を実装する。

完了条件:

- マイク音声からpartialとfinalの両方が得られる。
- partialの修正を`text`置換として処理できる。
- 切断後にsession memoryが解放される。

### Phase 7: VoiceDesignとクローン

実施:

- consent、voice metadata、参照音声保存を実装する。
- Base／VoiceDesignの副モデルスロットを実装する。
- VoiceDesign候補の承認フローを実装する。
- 音声プロファイルを`/v1/audio/speech`から利用可能にする。

完了条件:

- consentなしのクローン要求を拒否する。
- 副モデル解放後にメモリが通常時基準へ戻る。

### Phase 8: launchdと運用

実施:

- 起動scriptとplistを追加する。
- offline、warmup、Ready、graceful shutdownを接続する。
- status／doctor／smoke scriptへ音声項目を追加する。

完了条件:

- ログイン後に2デーモンが自動起動する。
- 一方を停止しても他方と既存LLM APIが利用できる。
- 異常終了後に当該デーモンだけが復帰する。

### Phase 9: 実機性能・品質選定

実施:

- ASR BF16と8bitを日本語fixtureで比較する。
- TTS BF16のメモリと速度を計測する。
- TTS/ASR同時実行と既存LLM idle共存を計測する。
- 30分連続streamと8時間idle常駐を試験する。

完了条件:

- 採用ASR形式が数値根拠とともにmanifestへ記録される。
- メモリ、swap、RTF、TTFAの受け入れ基準を満たす。

## 24. テスト計画

### 24.1 Unit

- schema validation
- OpenAI error shape
- `instructions`生成順序
- style preset
- audio format変換
- VAD state machine
- SSE event sequence
- WebSocket event sequence
- queue overflow
- disconnect cancellation
- model manifest不一致
- voice consent必須化

### 24.2 Integration

- fake modelによるFastAPI TestClient
- 実TTSモデル1リクエスト
- 実ASRモデル1リクエスト
- TTS PCM streaming
- ASR SSE
- ASR WebSocket
- ForcedAligner
- Base clone
- VoiceDesign候補生成

### 24.3 回帰

既存テスト:

```bash
./.venv/bin/python -m pytest tests
./embedding/.venv/bin/python -m pytest embedding/tests
```

音声テスト:

```bash
speech/.venv-tts/bin/python -m pytest speech/tests/test_tts_routes.py speech/tests/test_tts_streaming.py
speech/.venv-asr/bin/python -m pytest speech/tests/test_asr_routes.py speech/tests/test_asr_sse.py speech/tests/test_asr_websocket.py
```

実モデルsmoke:

```bash
speech/.venv-asr/bin/python scripts/smoke_speech_api.py \
  --tts-url http://127.0.0.1:44520 \
  --asr-url http://127.0.0.1:44521
```

## 25. 受け入れ基準

### 25.1 API

- OpenAI Python SDKからTTS RESTを呼べる。
- OpenAI Python SDKのstreaming responseからTTS音声を逐次取得できる。
- OpenAI Python/TypeScript SDKからASR RESTを呼べる。
- `stream=true`でASR SSEのpartial/finalを取得できる。
- WebSocketでbinary PCMを送ってpartial/finalを取得できる。
- `/v1/models`、`/live`、`/ready`、`/metrics`が両方に存在する。
- error responseがOpenAI形式で統一されている。

### 25.2 TTS

- 温間TTFA: 1.5秒以内
- 200文字程度の日本語でRTF 1.0未満
- 10回連続生成で失敗0
- 先頭欠落、文末切れ0
- Ono_Anna、少なくとも3種類のstyle presetが区別できる
- 自由形式instructionsが感情、速度、口調へ反映される
- VoiceDesignとBase cloneを同じデーモンで実行できる

### 25.3 ASR

- 10秒／60秒の日本語音声でRTF 0.5未満
- 発話終了からfinalまで2秒以内を目標とする
- 固有名詞promptが認識結果へ反映される
- 30分音声で停止しない
- word timestampが音声長の範囲内で単調増加する
- WebSocket 30分連続接続でメモリが増え続けない

### 25.4 常駐

- 再起動後、ネットワークなしでReadyになる。
- 一方の異常終了が他方と既存LLM APIへ波及しない。
- 通常メモリ16GB以内、オンデマンド時20GB以内。
- 30分負荷試験で継続的なswap増加がない。
- 8時間idleでRSSが増え続けない。

## 26. ロールバック

音声機能は既存LLM APIへrouterを追加せず独立させるため、ロールバックは次で完了できる。

1. 2つのlaunchd jobを停止する。
2. plistをunloadする。
3. 音声用Python環境を無効化する。
4. モデルは削除せず、revision付きディレクトリを保持する。
5. 既存LLM／Embeddingデーモンの状態を再確認する。

音声モデルや音声プロファイルの削除はロールバックに含めず、別の明示的な削除操作として扱う。

## 27. 主要リスク

| リスク | 対策 |
|---|---|
| `mlx-audio`、`mlx-qwen3-asr`が第三者実装 | runtime commit、model revision、lockfileを固定し、実音声fixtureを回帰試験する |
| 32GBで既存LLMと競合 | オンデマンドモデルを1本に制限し、通常16GB／ピーク20GBのgateを設ける |
| TTS streamの末尾欠落 | encoder flushとfinal chunkテストを必須化する |
| ASR partialの書き換わり | `delta`と置換用`text`を両方返し、completedだけを確定扱いにする |
| WebSocket送信過多 | 未処理音声上限、warning、1013 closeを実装する |
| クライアント切断後も推論継続 | disconnectをgenerator／session cancellationへ伝播する |
| 音声クローン悪用 | consent必須、管理scope、本文非ログ化、ローカル限定 |
| FastAPI環境の依存衝突 | TTS/ASRを専用Python 3.12環境へ分離する |

## 28. 参照

- [Qwen3-TTS公式リポジトリ](https://github.com/QwenLM/Qwen3-TTS)
- [Qwen3-ASR公式リポジトリ](https://github.com/QwenLM/Qwen3-ASR)
- [MLX Audio Qwen3-TTS実装](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/tts/models/qwen3_tts/README.md)
- [MLX Qwen3-ASR実装](https://github.com/moona3k/mlx-qwen3-asr)
- [OpenAI Text-to-Speech guide](https://developers.openai.com/api/docs/guides/text-to-speech)
- [OpenAI Audio guide](https://developers.openai.com/api/docs/guides/audio)
