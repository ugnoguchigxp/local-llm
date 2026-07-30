from __future__ import annotations

from fastapi.testclient import TestClient

from speech.asr.app import create_app
from speech.asr.backend import FakeASRBackend


def test_asr_ready_models_and_json(speech_settings, wav_bytes) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/v1/models").json()["data"][0]["backend"] == "fake"

        response = client.post(
            "/v1/audio/transcriptions",
            data={
                "model": "qwen3-asr-1.7b",
                "language": "Japanese",
                "response_format": "json",
            },
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )

    assert response.status_code == 200
    assert "Qwen3" in response.json()["text"]


def test_asr_verbose_timestamps_and_subtitles(speech_settings, wav_bytes) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    with TestClient(app) as client:
        verbose = client.post(
            "/v1/audio/transcriptions",
            data={
                "model": "qwen3-asr-1.7b",
                "language": "Japanese",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )
        srt = client.post(
            "/v1/audio/transcriptions",
            data={"model": "qwen3-asr-1.7b", "response_format": "srt"},
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )

    assert verbose.status_code == 200
    assert verbose.json()["words"]
    assert "segments" not in verbose.json()
    assert srt.status_code == 200
    assert "-->" in srt.text


def test_asr_sse(speech_settings, wav_bytes) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={
                "model": "qwen3-asr-1.7b",
                "stream": "true",
                "language": "Japanese",
            },
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )

    assert response.status_code == 200
    assert "event: transcript.started" in response.text
    assert "event: transcript.delta" in response.text
    assert "event: transcript.completed" in response.text
    assert "data: [DONE]" in response.text


def test_asr_translation_is_explicitly_unsupported(speech_settings) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    with TestClient(app) as client:
        response = client.post("/v1/audio/translations")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "translations_not_supported"


def test_asr_requires_known_model_and_nonempty_audio(
    speech_settings,
    wav_bytes,
) -> None:
    app = create_app(settings=speech_settings, backend=FakeASRBackend())
    with TestClient(app) as client:
        missing = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )
        unknown = client.post(
            "/v1/audio/transcriptions",
            data={"model": "unknown-asr"},
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )
        empty = client.post(
            "/v1/audio/transcriptions",
            data={"model": "qwen3-asr-1.7b"},
            files={"file": ("empty.wav", b"", "audio/wav")},
        )

    assert missing.status_code == 422
    assert missing.json()["error"]["param"] == "model"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "empty_audio"


def test_asr_sse_reuses_accurate_timestamp_result(
    speech_settings,
    wav_bytes,
) -> None:
    class CountingBackend(FakeASRBackend):
        def __init__(self) -> None:
            super().__init__()
            self.transcribe_calls = 0

        def transcribe(self, *args, **kwargs):
            self.transcribe_calls += 1
            return super().transcribe(*args, **kwargs)

    backend = CountingBackend()
    app = create_app(settings=speech_settings, backend=backend)
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={
                "model": "qwen3-asr-1.7b",
                "stream": "true",
                "timestamp_granularities[]": "word",
            },
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
        )

    assert response.status_code == 200
    assert '"words":' in response.text
    assert backend.transcribe_calls == 1
