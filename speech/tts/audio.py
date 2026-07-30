from __future__ import annotations

import io
import shutil
import struct
import subprocess
import tempfile
import wave
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from speech.common.errors import SpeechAPIError

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "application/octet-stream",
}


def float_to_pcm16(audio: np.ndarray) -> bytes:
    values = np.asarray(audio, dtype=np.float32).reshape(-1)
    values = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
    values = np.clip(values, -1.0, 1.0)
    return (values * 32767.0).astype("<i2", copy=False).tobytes()


def streaming_wav_header(sample_rate: int, channels: int = 1) -> bytes:
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", 0xFFFFFFFF),
            b"WAVEfmt ",
            struct.pack(
                "<IHHIIHH",
                16,
                1,
                channels,
                sample_rate,
                byte_rate,
                block_align,
                bits_per_sample,
            ),
            b"data",
            struct.pack("<I", 0xFFFFFFFF),
        )
    )


def encode_buffered(
    chunks: Iterable[np.ndarray],
    *,
    sample_rate: int,
    response_format: str,
) -> bytes:
    pcm = b"".join(float_to_pcm16(chunk) for chunk in chunks)
    if response_format == "pcm":
        return pcm
    if response_format == "wav":
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return buffer.getvalue()
    return _encode_with_ffmpeg(pcm, sample_rate, response_format)


def ffmpeg_stream_args(sample_rate: int, response_format: str) -> list[str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise SpeechAPIError(
            503,
            "ffmpeg is required for compressed audio output",
            "ffmpeg_not_installed",
            "service_unavailable_error",
        )
    codec_args: dict[str, list[str]] = {
        "mp3": ["-f", "mp3", "-codec:a", "libmp3lame"],
        "opus": ["-f", "ogg", "-codec:a", "libopus"],
        "aac": ["-f", "adts", "-codec:a", "aac"],
        "flac": ["-f", "flac", "-codec:a", "flac"],
    }
    if response_format not in codec_args:
        raise ValueError(f"unsupported ffmpeg format: {response_format}")
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        *codec_args[response_format],
        "pipe:1",
    ]


def _encode_with_ffmpeg(pcm: bytes, sample_rate: int, response_format: str) -> bytes:
    if response_format == "flac":
        with tempfile.TemporaryDirectory(prefix="qwen3-tts-flac-") as directory:
            output_path = Path(directory) / "audio.flac"
            command = ffmpeg_stream_args(sample_rate, response_format)
            command[-1] = str(output_path)
            completed = subprocess.run(
                command,
                input=pcm,
                capture_output=True,
                check=False,
                timeout=300,
            )
            _raise_for_ffmpeg_error(completed)
            return output_path.read_bytes()

    completed = subprocess.run(
        ffmpeg_stream_args(sample_rate, response_format),
        input=pcm,
        capture_output=True,
        check=False,
        timeout=300,
    )
    _raise_for_ffmpeg_error(completed)
    return completed.stdout


def _raise_for_ffmpeg_error(
    completed: subprocess.CompletedProcess[bytes],
) -> None:
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpeechAPIError(
            500,
            f"audio encoding failed: {message or completed.returncode}",
            "audio_encoding_failed",
            "server_error",
        )
