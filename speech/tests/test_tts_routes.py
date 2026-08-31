from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from speech.tts.app import create_app
from speech.tts.backend import FakeTTSBackend
from speech.tts.voices import VoiceStore


def test_tts_ready_models_and_pcm_stream(speech_settings) -> None:
    backend = FakeTTSBackend()
    app = create_app(settings=speech_settings, backend=backend)

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        models = client.get("/v1/models").json()
        assert models["data"][0]["backend"] == "fake"

        with client.stream(
            "POST",
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "音声ストリーミングのテストです。",
                "voice": "ono_anna",
                "response_format": "pcm",
            },
        ) as response:
            chunks = list(response.iter_bytes())

        assert response.status_code == 200
        assert response.headers["x-audio-sample-rate"] == "24000"
        assert response.headers["x-audio-encoding"] == "pcm_s16le"
        assert sum(len(chunk) for chunk in chunks) > 1000


def test_tts_wav_response_has_streaming_header(speech_settings) -> None:
    app = create_app(settings=speech_settings, backend=FakeTTSBackend())
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "WAV出力です。",
                "voice": "ono_anna",
                "response_format": "wav",
            },
        )

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")
    assert b"WAVE" in response.content[:16]


def test_tts_rejects_unknown_voice_profile(speech_settings) -> None:
    app = create_app(settings=speech_settings, backend=FakeTTSBackend())
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "テスト",
                "voice": "voice_missing",
                "response_format": "pcm",
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "voice_not_found"


def test_voice_consent_profile_and_design(speech_settings, wav_bytes) -> None:
    store = VoiceStore(speech_settings.data_root / "voices")
    app = create_app(
        settings=speech_settings,
        backend=FakeTTSBackend(),
        voice_store=store,
    )
    with TestClient(app) as client:
        consent = client.post(
            "/v1/audio/voice_consents",
            data={
                "name": "test consent",
                "language": "ja",
                "owner": "tester",
                "usage_scope": "tests",
            },
            files={"recording": ("consent.wav", wav_bytes, "audio/wav")},
        )
        assert consent.status_code == 200

        voice = client.post(
            "/v1/audio/voices",
            data={
                "name": "test voice",
                "reference_text": "これは参照音声です。",
                "consent": consent.json()["id"],
                "language": "Japanese",
            },
            files={"audio_sample": ("sample.wav", wav_bytes, "audio/wav")},
        )
        assert voice.status_code == 200
        voice_id = voice.json()["id"]

        speech = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-1.7b",
                "input": "登録音声のテストです。",
                "voice": {"id": voice_id},
                "response_format": "pcm",
            },
        )
        assert speech.status_code == 200
        assert len(speech.content) > 1000

        designed = client.post(
            "/v1/audio/voices/design",
            json={
                "name": "designed",
                "description": "穏やかな日本語女性音声",
                "preview_text": "候補音声です。",
                "language": "Japanese",
                "candidates": 1,
            },
        )
        assert designed.status_code == 200
        assert designed.json()["data"][0]["source"] == "voice_design"


def test_tts_requires_known_model_and_valid_style_preset(speech_settings) -> None:
    app = create_app(settings=speech_settings, backend=FakeTTSBackend())
    with TestClient(app) as client:
        missing = client.post(
            "/v1/audio/speech",
            json={"input": "テスト", "voice": "ono_anna"},
        )
        unknown = client.post(
            "/v1/audio/speech",
            json={
                "model": "unknown-tts",
                "input": "テスト",
                "voice": "ono_anna",
            },
        )
        bad_style = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "テスト",
                "voice": "ono_anna",
                "qwen": {"style_preset": "not-a-preset"},
            },
        )

    assert missing.status_code == 422
    assert missing.json()["error"]["param"] == "model"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    assert bad_style.status_code == 422
    assert bad_style.json()["error"]["param"] == "qwen.style_preset"


def test_tts_unhandled_backend_error_has_openai_shape(speech_settings) -> None:
    class BrokenBackend(FakeTTSBackend):
        def stream(self, request, voice_profile=None):
            del request, voice_profile
            raise RuntimeError("backend internals must not leak")

    app = create_app(settings=speech_settings, backend=BrokenBackend())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "テスト",
                "voice": "ono_anna",
                "response_format": "pcm",
            },
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "message": "An internal server error occurred",
            "type": "server_error",
            "param": None,
            "code": "internal_server_error",
        }
    }


def test_tts_buffered_flac_has_duration_metadata(
    speech_settings,
    tmp_path,
) -> None:
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe is not installed")
    app = create_app(settings=speech_settings, backend=FakeTTSBackend())
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "FLAC出力です。",
                "voice": "ono_anna",
                "response_format": "flac",
            },
        )

    output = tmp_path / "output.flac"
    output.write_bytes(response.content)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(response.content)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 0
