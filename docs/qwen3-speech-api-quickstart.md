# Qwen3 Speech API Quickstart

このプロジェクトでは、TTS（テキスト読み上げ）とASR（音声認識）を、独立したローカルAPIとして利用できます。

| 用途 | Base URL | モデル |
|---|---|---|
| TTS | `http://127.0.0.1:44520/v1` | `qwen3-tts-1.7b-custom-voice` |
| ASR | `http://127.0.0.1:44521/v1` | `qwen3-asr-1.7b` |

## 1. 起動確認

```bash
./scripts/speech_launchd status all

curl http://127.0.0.1:44520/ready
curl http://127.0.0.1:44521/ready
```

`"status": "ready"`なら利用可能です。再起動する場合は次を実行します。

```bash
./scripts/speech_launchd restart all
```

一通りの実モデル疎通確認：

```bash
speech/.venv-asr/bin/python scripts/smoke_speech_api.py
```

## 2. TTS：テキストから音声を作る

### curl

```bash
curl -sS http://127.0.0.1:44520/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3-tts-1.7b-custom-voice",
    "input": "こんにちは。これはQwen3 TTSのテストです。",
    "voice": "ono_anna",
    "instructions": "落ち着いて、明瞭に話してください。",
    "response_format": "mp3",
    "speed": 1.0
  }' \
  --output speech.mp3
```

生成した音声は通常のプレイヤーで再生できます。

```bash
open speech.mp3
```

利用できる主なプリセット音声は、`GET /v1/models`の`speakers`で確認できます。

```bash
curl http://127.0.0.1:44520/v1/models
```

対応形式：

- ストリーミング：`pcm`, `wav`, `mp3`, `opus`
- 完成後に返却：`aac`, `flac`

### 話し方を細かく指定する

`qwen`はこのローカルAPI独自の拡張フィールドです。

```json
{
  "model": "qwen3-tts-1.7b-custom-voice",
  "input": "本日の予定をご案内します。",
  "voice": "ono_anna",
  "instructions": "自然な案内音声として話してください。",
  "response_format": "wav",
  "speed": 0.95,
  "qwen": {
    "language": "Japanese",
    "style_preset": "calm_narration",
    "style": {
      "emotion": "穏やか",
      "pace": "少しゆっくり",
      "pitch": "やや低め",
      "energy": "控えめ",
      "intonation": "自然",
      "pause": "句読点で明瞭"
    },
    "seed": 1234
  }
}
```

`style_preset`は次の3種類です。

- `calm_narration`
- `friendly_agent`
- `urgent_notice`

## 3. ASR：音声ファイルを文字起こしする

### 通常のJSON応答

```bash
curl -sS http://127.0.0.1:44521/v1/audio/transcriptions \
  -F 'model=qwen3-asr-1.7b' \
  -F 'language=Japanese' \
  -F 'response_format=json' \
  -F 'file=@speech.mp3'
```

応答例：

```json
{
  "text": "こんにちは。これはQwen3 TTSのテストです。"
}
```

### word timestamp付き

```bash
curl -sS http://127.0.0.1:44521/v1/audio/transcriptions \
  -F 'model=qwen3-asr-1.7b' \
  -F 'language=Japanese' \
  -F 'response_format=verbose_json' \
  -F 'timestamp_granularities[]=word' \
  -F 'file=@speech.mp3'
```

出力形式は`json`, `text`, `verbose_json`, `srt`, `vtt`に対応しています。

### SSEストリーミング

```bash
curl -N http://127.0.0.1:44521/v1/audio/transcriptions \
  -F 'model=qwen3-asr-1.7b' \
  -F 'language=Japanese' \
  -F 'stream=true' \
  -F 'file=@speech.mp3'
```

`transcript.delta`が途中結果、`transcript.completed`が確定結果です。

リアルタイムマイク入力では、次のWebSocket endpointも利用できます。

```text
ws://127.0.0.1:44521/v1/audio/transcriptions/stream
```

入力は16kHz・モノラル・PCM16です。具体的な接続例は
[`scripts/smoke_speech_api.py`](../scripts/smoke_speech_api.py)を参照してください。

## 4. OpenAI Python SDKから使う

```bash
pip install openai
```

```python
from openai import OpenAI

tts = OpenAI(
    base_url="http://127.0.0.1:44520/v1",
    api_key="local",
)

with tts.audio.speech.with_streaming_response.create(
    model="qwen3-tts-1.7b-custom-voice",
    voice="ono_anna",
    input="OpenAI SDKから音声を生成しています。",
    response_format="mp3",
) as response:
    response.stream_to_file("speech.mp3")

asr = OpenAI(
    base_url="http://127.0.0.1:44521/v1",
    api_key="local",
)

with open("speech.mp3", "rb") as audio:
    result = asr.audio.transcriptions.create(
        model="qwen3-asr-1.7b",
        file=audio,
        language="Japanese",
        response_format="verbose_json",
        timestamp_granularities=["word"],
    )

print(result.text)
```

## 5. Bearer認証を有効にしている場合

`.env`で次を設定した場合、APIリクエストにBearer tokenが必要です。

```dotenv
LOCAL_LLM_REQUIRE_AUTH=true
LOCAL_LLM_ACCESS_TOKEN=任意の十分長いトークン
```

curlでは次のheaderを追加します。

```bash
-H "Authorization: Bearer ${LOCAL_LLM_ACCESS_TOKEN}"
```

OpenAI SDKでは`api_key`へ同じtokenを指定してください。

## 6. 音声設計・音声クローン

追加APIとして次も利用できます。

| Endpoint | 用途 |
|---|---|
| `POST /v1/audio/voice_consents` | クローン利用への同意音声を登録 |
| `POST /v1/audio/voices` | 参照音声からvoice profileを作成 |
| `POST /v1/audio/voices/design` | 説明文から新しい音声を設計 |
| `GET /v1/audio/voices` | 登録済みvoice一覧 |
| `DELETE /v1/audio/voices/{voice_id}` | voiceを回復可能なtrashへ移動 |

登録後の`voice_*` IDは、通常の`POST /v1/audio/speech`の`voice`へ指定できます。
