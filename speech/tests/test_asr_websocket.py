from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from speech.asr.app import create_app
from speech.asr.backend import FakeASRBackend


def test_asr_websocket_binary_pcm_partial_and_final(speech_settings) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    sample_rate = 16000
    indexes = np.arange(sample_rate, dtype=np.float32)
    speech = (0.2 * np.sin(2 * np.pi * 440 * indexes / sample_rate) * 32767).astype(
        "<i2"
    )
    silence = np.zeros(int(sample_rate * 0.8), dtype="<i2")

    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/audio/transcriptions/stream") as websocket,
    ):
        created = websocket.receive_json()
        assert created["type"] == "transcription_session.created"

        websocket.send_json(
            {
                "type": "transcription_session.update",
                "session": {
                    "language": "Japanese",
                    "input_audio_format": "pcm16",
                    "sample_rate": 16000,
                    "vad": {
                        "enabled": True,
                        "threshold": 0.01,
                        "speech_start_ms": 100,
                        "speech_end_ms": 500,
                        "pre_roll_ms": 200,
                        "max_utterance_seconds": 30,
                    },
                },
            }
        )
        assert websocket.receive_json()["type"] == "transcription_session.updated"

        websocket.send_bytes(speech.tobytes())
        assert websocket.receive_json()["type"] == "input_audio_buffer.speech_started"
        delta = websocket.receive_json()
        assert delta["type"] == "conversation.item.input_audio_transcription.delta"

        websocket.send_bytes(silence.tobytes())
        next_event = websocket.receive_json()
        if next_event["type"] == "conversation.item.input_audio_transcription.delta":
            next_event = websocket.receive_json()
        assert next_event["type"] == "input_audio_buffer.speech_stopped"
        completed = websocket.receive_json()
        assert completed["type"] == (
            "conversation.item.input_audio_transcription.completed"
        )
        assert completed["transcript"]

        websocket.send_json({"type": "session.close"})
        assert websocket.receive_json()["type"] == "session.completed"


def test_asr_websocket_rejects_bad_events_without_dropping_session(
    speech_settings,
) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())

    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/audio/transcriptions/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "transcription_session.created"

        websocket.send_text("[]")
        invalid_object = websocket.receive_json()
        assert invalid_object["error"]["code"] == "invalid_event"

        websocket.send_json({"type": "input_audio_buffer.append", "audio": "***"})
        invalid_base64 = websocket.receive_json()
        assert invalid_base64["error"]["code"] == "invalid_audio_base64"

        websocket.send_bytes(b"\x00")
        invalid_pcm = websocket.receive_json()
        assert invalid_pcm["error"]["code"] == "invalid_audio_frame"

        websocket.send_json(
            {
                "type": "transcription_session.update",
                "session": {"sample_rate": 8000},
            }
        )
        invalid_schema = websocket.receive_json()
        assert invalid_schema["error"]["code"] == "invalid_event"

        websocket.send_json({"type": "session.close"})
        assert websocket.receive_json()["type"] == "session.completed"


def test_asr_websocket_returns_requested_final_timestamps(
    speech_settings,
) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    pcm = np.zeros(16000, dtype="<i2").tobytes()

    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/audio/transcriptions/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "transcription_session.created"
        websocket.send_json(
            {
                "type": "transcription_session.update",
                "session": {
                    "language": "Japanese",
                    "timestamp_granularities": ["segment", "word"],
                    "vad": {"enabled": False},
                },
            }
        )
        assert websocket.receive_json()["type"] == "transcription_session.updated"

        websocket.send_bytes(pcm)
        assert websocket.receive_json()["type"] == "input_audio_buffer.speech_started"
        websocket.send_json({"type": "input_audio_buffer.commit"})
        completed = websocket.receive_json()
        if completed["type"].endswith(".delta"):
            completed = websocket.receive_json()

        assert completed["type"].endswith(".completed")
        assert completed["segments"]
        assert completed["words"]
        assert completed["duration_ms"] == 1000
        assert completed["processing_ms"] >= 0

        websocket.send_json({"type": "session.close"})
        assert websocket.receive_json()["type"] == "session.completed"
