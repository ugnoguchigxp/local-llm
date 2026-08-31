#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import wave
from pathlib import Path

import httpx
import websockets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test Qwen3 speech APIs.")
    parser.add_argument("--tts-url", default="http://127.0.0.1:44520")
    parser.add_argument("--asr-url", default="http://127.0.0.1:44521")
    parser.add_argument("--token", default=os.getenv("LOCAL_LLM_ACCESS_TOKEN", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(timeout=300, headers=headers) as client:
        tts_ready = wait_ready(client, args.tts_url, service="TTS")
        asr_ready = wait_ready(client, args.asr_url, service="ASR")
        print("TTS ready:", tts_ready.json()["status"])
        print("ASR ready:", asr_ready.json()["status"])

        started = time.monotonic()
        first_chunk_seconds = None
        pcm_parts: list[bytes] = []
        with client.stream(
            "POST",
            f"{args.tts_url}/v1/audio/speech",
            json={
                "model": "qwen3-tts-0.6b-custom-voice",
                "input": "音声認識のストリーミングテストです。",
                "voice": "ono_anna",
                "instructions": "落ち着いて明瞭に話してください。",
                "response_format": "pcm",
            },
        ) as response:
            response.raise_for_status()
            sample_rate = int(response.headers["X-Audio-Sample-Rate"])
            for chunk in response.iter_bytes():
                if chunk and first_chunk_seconds is None:
                    first_chunk_seconds = time.monotonic() - started
                pcm_parts.append(chunk)

        pcm = b"".join(pcm_parts)
        if not pcm:
            raise RuntimeError("TTS stream returned no audio")
        print(
            "TTS stream:",
            f"bytes={len(pcm)}",
            f"ttfa={first_chunk_seconds:.3f}s",
        )

        with tempfile.TemporaryDirectory(prefix="speech-smoke-") as directory:
            wav_path = Path(directory) / "tts.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm)

            with wav_path.open("rb") as audio_file:
                transcription = client.post(
                    f"{args.asr_url}/v1/audio/transcriptions",
                    data={
                        "model": "qwen3-asr-1.7b",
                        "language": "Japanese",
                        "response_format": "verbose_json",
                        "timestamp_granularities": "word",
                    },
                    files={"file": ("tts.wav", audio_file, "audio/wav")},
                )
            transcription.raise_for_status()
            payload = transcription.json()
            if not payload.get("text"):
                raise RuntimeError("ASR REST returned an empty transcript")
            if not payload.get("words"):
                raise RuntimeError("ASR REST did not return requested word timestamps")
            print("ASR REST:", payload["text"])

            with (
                wav_path.open("rb") as audio_file,
                client.stream(
                    "POST",
                    f"{args.asr_url}/v1/audio/transcriptions",
                    data={
                        "model": "qwen3-asr-1.7b",
                        "language": "Japanese",
                        "stream": "true",
                        "timestamp_granularities": "word",
                    },
                    files={"file": ("tts.wav", audio_file, "audio/wav")},
                ) as response,
            ):
                response.raise_for_status()
                sse_text = "".join(response.iter_text())
            if "transcript.completed" not in sse_text or "[DONE]" not in sse_text:
                raise RuntimeError("ASR SSE did not complete")
            if '"words":' not in sse_text:
                raise RuntimeError("ASR SSE did not return requested word timestamps")
            print("ASR SSE: completed")

            asyncio.run(
                websocket_smoke(
                    args.asr_url,
                    pcm,
                    sample_rate=sample_rate,
                    token=args.token,
                )
            )
    return 0


def wait_ready(
    client: httpx.Client,
    base_url: str,
    *,
    service: str,
    timeout_seconds: float = 180.0,
) -> httpx.Response:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{base_url}/ready")
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}: {response.text}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"{service} did not become ready: {last_error or 'timeout'}")


async def websocket_smoke(
    asr_url: str,
    pcm: bytes,
    *,
    sample_rate: int,
    token: str,
) -> None:
    if sample_rate != 16000:
        pcm = await asyncio.to_thread(resample_pcm, pcm, sample_rate, 16000)
    ws_url = asr_url.replace("http://", "ws://").replace("https://", "wss://")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with websockets.connect(
        f"{ws_url}/v1/audio/transcriptions/stream",
        additional_headers=headers,
        max_size=4 * 1024 * 1024,
    ) as websocket:
        created = json.loads(await websocket.recv())
        if created.get("type") != "transcription_session.created":
            raise RuntimeError(f"unexpected WebSocket first event: {created}")
        await websocket.send(
            json.dumps(
                {
                    "type": "transcription_session.update",
                    "session": {
                        "language": "Japanese",
                        "prompt": "Qwen3 MLX",
                        "input_audio_format": "pcm16",
                        "sample_rate": 16000,
                        "timestamp_granularities": ["word"],
                        "vad": {"enabled": False},
                    },
                },
                ensure_ascii=False,
            )
        )
        await websocket.recv()
        for offset in range(0, len(pcm), 3200):
            await websocket.send(pcm[offset : offset + 3200])
        await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
        completed = None
        for _ in range(50):
            event = json.loads(await websocket.recv())
            if event.get("type", "").endswith("completed"):
                completed = event
                break
        if not completed or not completed.get("transcript"):
            raise RuntimeError("ASR WebSocket did not return a final transcript")
        if not completed.get("words"):
            raise RuntimeError(
                "ASR WebSocket did not return requested word timestamps"
            )
        print("ASR WebSocket:", completed["transcript"])
        await websocket.send(json.dumps({"type": "session.close"}))


def resample_pcm(
    pcm: bytes,
    source_rate: int,
    target_rate: int,
) -> bytes:
    import numpy as np

    source = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    target_length = round(len(source) * target_rate / source_rate)
    source_positions = np.arange(len(source), dtype=np.float64)
    target_positions = np.linspace(0, max(0, len(source) - 1), target_length)
    target = np.interp(target_positions, source_positions, source)
    return np.clip(target, -32768, 32767).astype("<i2").tobytes()


if __name__ == "__main__":
    raise SystemExit(main())
