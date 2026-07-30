from __future__ import annotations

import html
from pathlib import Path

import numpy as np

from speech.asr.schemas import ASRResult

ALLOWED_SUFFIXES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
    ".mp4",
    ".mov",
}


def safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SUFFIXES else ".audio"


def pcm16_bytes_to_float(data: bytes) -> np.ndarray:
    if len(data) % 2:
        raise ValueError("PCM16 audio must contain complete 16-bit samples")
    if not data:
        return np.array([], dtype=np.float32)
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def result_payload(result: ASRResult, response_format: str) -> tuple[bytes, str]:
    if response_format == "text":
        return result.text.encode("utf-8"), "text/plain; charset=utf-8"
    if response_format == "srt":
        return _srt(result).encode("utf-8"), "application/x-subrip; charset=utf-8"
    if response_format == "vtt":
        return _vtt(result).encode("utf-8"), "text/vtt; charset=utf-8"
    raise ValueError(f"result_payload does not encode JSON format: {response_format}")


def verbose_json(
    result: ASRResult,
    granularities: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task": "transcribe",
        "language": result.language,
        "duration": result.duration,
        "text": result.text,
        "finish_reason": result.finish_reason,
        "truncated": result.truncated,
    }
    payload.update(timestamp_fields(result, granularities))
    return payload


def timestamp_fields(
    result: ASRResult,
    granularities: list[str] | None = None,
) -> dict[str, object]:
    requested = set(granularities or ())
    include_segments = not requested or "segment" in requested
    include_words = "word" in requested
    payload: dict[str, object] = {}
    if include_segments:
        payload["segments"] = _openai_segments(_segment_values(result))
    if include_words:
        payload["words"] = _openai_words(result.words)
    return payload


def _openai_segments(
    segments: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "id": int(item.get("chunk_index", index)),
            "start": float(item.get("start", 0.0)),
            "end": float(item.get("end", 0.0)),
            "text": str(item.get("text", "")),
        }
        for index, item in enumerate(segments)
    ]


def _openai_words(
    words: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "word": str(item.get("text", item.get("word", ""))),
            "start": float(item.get("start", 0.0)),
            "end": float(item.get("end", 0.0)),
        }
        for item in words
    ]


def _srt(result: ASRResult) -> str:
    segments = _segment_values(result) or [
        {"text": result.text, "start": 0.0, "end": result.duration}
    ]
    blocks = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{_timestamp(float(segment.get('start', 0.0)), srt=True)} --> "
            f"{_timestamp(float(segment.get('end', result.duration)), srt=True)}\n"
            f"{segment.get('text', '')}"
        )
    return "\n\n".join(blocks) + "\n"


def _vtt(result: ASRResult) -> str:
    segments = _segment_values(result) or [
        {"text": result.text, "start": 0.0, "end": result.duration}
    ]
    blocks = ["WEBVTT"]
    for segment in segments:
        blocks.append(
            f"{_timestamp(float(segment.get('start', 0.0)), srt=False)} --> "
            f"{_timestamp(float(segment.get('end', result.duration)), srt=False)}\n"
            f"{html.escape(str(segment.get('text', '')))}"
        )
    return "\n\n".join(blocks) + "\n"


def _segment_values(result: ASRResult) -> list[dict[str, object]]:
    if not result.words:
        return result.segments
    return _group_words(result.words, language=result.language)


def _group_words(
    words: list[dict[str, object]],
    *,
    language: str,
) -> list[dict[str, object]]:
    grouped: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    for raw in words:
        text = str(raw.get("text", raw.get("word", ""))).strip()
        if not text:
            continue
        start = float(raw.get("start", 0.0))
        end = max(start, float(raw.get("end", start)))
        item: dict[str, object] = {"text": text, "start": start, "end": end}
        if current and _should_break_segment(current, item, language=language):
            grouped.append(_joined_segment(current, language=language))
            current = []
        current.append(item)
    if current:
        grouped.append(_joined_segment(current, language=language))
    return grouped


def _should_break_segment(
    current: list[dict[str, object]],
    item: dict[str, object],
    *,
    language: str,
) -> bool:
    start = float(item["start"])
    end = float(item["end"])
    gap = start - float(current[-1]["end"])
    duration = end - float(current[0]["start"])
    candidate = _join_tokens([*current, item], language=language)
    return (
        gap >= 0.8
        or duration > 6.0
        or len(current) >= 10
        or len(candidate) > 42
        or str(current[-1]["text"]).endswith((".", "!", "?", "。", "！", "？"))
    )


def _joined_segment(
    words: list[dict[str, object]],
    *,
    language: str,
) -> dict[str, object]:
    return {
        "text": _join_tokens(words, language=language),
        "start": float(words[0]["start"]),
        "end": float(words[-1]["end"]),
    }


def _join_tokens(
    words: list[dict[str, object]],
    *,
    language: str,
) -> str:
    normalized = language.strip().lower()
    separator = (
        ""
        if normalized.startswith(("ja", "zh", "ko"))
        or normalized in {"japanese", "chinese", "korean"}
        else " "
    )
    return separator.join(str(item["text"]).strip() for item in words)


def _timestamp(seconds: float, *, srt: bool) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"
