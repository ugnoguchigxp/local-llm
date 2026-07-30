from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from api.auth import require_api_auth
from speech.asr.audio import (
    pcm16_bytes_to_float,
    result_payload,
    safe_suffix,
    timestamp_fields,
    verbose_json,
)
from speech.asr.backend import ASRBackend, ASRStream, create_asr_backend
from speech.asr.schemas import (
    ASRResult,
    AudioAppendEvent,
    ControlEvent,
    SessionUpdateEvent,
    StreamUpdate,
    TranscriptionSessionSettings,
)
from speech.asr.vad import EnergyVAD
from speech.common.async_utils import DeferredCleanup, to_thread_cancel_safe
from speech.common.errors import SpeechAPIError, install_error_handlers
from speech.common.gate import InferenceGate
from speech.common.metrics import Metrics
from speech.common.settings import CommonSettings, asr_settings
from speech.common.websocket_auth import EphemeralTokenStore, websocket_authorized

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_WEBSOCKET_AUDIO_BYTES = 4 * 1024 * 1024
SUPPORTED_ASR_MODELS = frozenset({"qwen3-asr-1.7b"})
logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: CommonSettings | None = None,
    backend: ASRBackend | None = None,
) -> FastAPI:
    resolved_settings = settings or asr_settings()
    resolved_backend = backend or create_asr_backend(resolved_settings)
    resolved_settings.ensure_directories()
    gate = InferenceGate(
        concurrency=1,
        queue_size=resolved_settings.queue_size,
        timeout_seconds=resolved_settings.inference_timeout_seconds,
    )
    metrics = Metrics("qwen3-asr")
    token_store = EphemeralTokenStore(ttl_seconds=60)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ready = False
        application.state.startup_error = None
        if resolved_settings.preload:
            try:
                await asyncio.to_thread(resolved_backend.load)
                if _warmup_enabled(resolved_settings.fake_backend):
                    await asyncio.to_thread(resolved_backend.warmup)
                application.state.ready = True
            except Exception as exc:
                application.state.startup_error = str(exc)
                if resolved_settings.fake_backend:
                    raise
        yield
        application.state.ready = False
        await asyncio.to_thread(resolved_backend.close)

    app = FastAPI(
        title="Qwen3 ASR OpenAI-Compatible API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    app.state.startup_error = None
    app.state.backend = resolved_backend
    app.state.settings = resolved_settings
    app.state.gate = gate
    app.state.metrics = metrics
    app.state.token_store = token_store
    install_error_handlers(app)

    @app.get("/live")
    async def live() -> dict[str, object]:
        return {"status": "ok", "service": "qwen3-asr"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        is_ready = bool(app.state.ready and resolved_backend.ready)
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "service": "qwen3-asr",
                "model": resolved_backend.model_info() if is_ready else None,
                "error": app.state.startup_error,
                "queue": {"active": gate.active, "waiting": gate.waiting},
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if app.state.ready else "degraded",
            "service": "qwen3-asr",
            "ready": bool(app.state.ready),
            "queue": {"active": gate.active, "waiting": gate.waiting},
            "model": resolved_backend.model_info() if resolved_backend.ready else None,
        }

    @app.get("/metrics", response_class=Response)
    async def prometheus_metrics() -> Response:
        metrics.set("queue_active", gate.active)
        metrics.set("queue_waiting", gate.waiting)
        return Response(metrics.prometheus(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/models", dependencies=[Depends(require_api_auth)])
    async def models() -> dict[str, object]:
        return {"object": "list", "data": [resolved_backend.model_info()]}

    @app.post(
        "/v1/audio/transcription_sessions",
        dependencies=[Depends(require_api_auth)],
    )
    async def create_transcription_session() -> dict[str, object]:
        token, expires = token_store.issue()
        return {
            "id": f"session_{uuid.uuid4().hex[:24]}",
            "object": "audio.transcription_session",
            "client_secret": {"value": token, "expires_in": expires},
        }

    @app.post(
        "/v1/audio/transcriptions",
        dependencies=[Depends(require_api_auth)],
    )
    async def transcriptions(
        file: UploadFile = File(...),
        model: str = Form(...),
        language: str | None = Form(None),
        prompt: str = Form(""),
        response_format: str = Form("json"),
        temperature: float = Form(0.0),
        timestamp_granularities: list[str] | None = Form(None),
        timestamp_granularities_bracket: list[str] | None = Form(
            None,
            alias="timestamp_granularities[]",
        ),
        stream: bool = Form(False),
    ):
        _require_model(model)
        _require_ready(app)
        if response_format not in {"json", "text", "verbose_json", "srt", "vtt"}:
            raise SpeechAPIError(
                400,
                f"unsupported response_format: {response_format}",
                "invalid_response_format",
                param="response_format",
            )
        if temperature != 0.0:
            raise SpeechAPIError(
                400,
                "Qwen3-ASR uses deterministic decoding; temperature must be 0",
                "unsupported_temperature",
                param="temperature",
            )
        granularities = [
            *(timestamp_granularities or []),
            *(timestamp_granularities_bracket or []),
        ]
        invalid = [item for item in granularities if item not in {"segment", "word"}]
        if invalid:
            raise SpeechAPIError(
                400,
                f"unsupported timestamp granularity: {invalid[0]}",
                "invalid_timestamp_granularity",
                param="timestamp_granularities",
            )

        temp_path = await _save_upload(
            file,
            directory=resolved_settings.data_root / "tmp",
            max_bytes=MAX_UPLOAD_BYTES,
        )
        request_id = f"asr_{uuid.uuid4().hex[:24]}"
        try:
            await gate.acquire()
        except BaseException:
            _unlink(temp_path)
            raise
        metrics.inc("asr_requests_total")
        started = time.monotonic()

        if stream:
            iterator: Iterator[StreamUpdate] | None = None

            async def cleanup_stream() -> None:
                try:
                    if iterator is not None:
                        _close_iterator(iterator)
                finally:
                    await gate.release()
                    _unlink(temp_path)

            cleanup = DeferredCleanup(cleanup_stream)

            async def events() -> AsyncIterator[str]:
                nonlocal iterator
                previous = ""
                sequence = 0
                try:
                    yield _sse(
                        "transcript.started",
                        {"request_id": request_id, "sequence": sequence},
                    )
                    iterator = resolved_backend.stream_file(
                        temp_path,
                        language=language,
                        prompt=prompt,
                        timestamps=bool(granularities),
                    )
                    final_update: StreamUpdate | None = None
                    while True:
                        update = await to_thread_cancel_safe(
                            _next_update,
                            iterator,
                            deferred_cleanup=cleanup,
                        )
                        if update is None:
                            break
                        final_update = update
                        sequence += 1
                        delta = _text_delta(previous, update.text)
                        previous = update.text
                        event_name = (
                            "transcript.completed"
                            if update.is_final
                            else "transcript.delta"
                        )
                        payload: dict[str, object] = {
                            "request_id": request_id,
                            "sequence": sequence,
                            "delta": delta,
                            "text": update.text,
                            "language": update.language,
                        }
                        if update.is_final and granularities:
                            if update.segments is None or update.words is None:
                                aligned = await to_thread_cancel_safe(
                                    resolved_backend.transcribe,
                                    temp_path,
                                    language=language,
                                    prompt=prompt,
                                    timestamps=True,
                                    deferred_cleanup=cleanup,
                                )
                            else:
                                aligned = _result_from_update(update)
                            payload.update(timestamp_fields(aligned, granularities))
                        yield _sse(event_name, payload)
                    if final_update is None or not final_update.is_final:
                        sequence += 1
                        yield _sse(
                            "transcript.completed",
                            {
                                "request_id": request_id,
                                "sequence": sequence,
                                "text": previous,
                                "language": language or "unknown",
                            },
                        )
                    yield "data: [DONE]\n\n"
                except asyncio.CancelledError:
                    metrics.inc("asr_stream_cancellations_total")
                    raise
                except SpeechAPIError as exc:
                    metrics.inc("asr_errors_total")
                    yield _sse(
                        "error",
                        {
                            "request_id": request_id,
                            "error": {
                                "message": exc.message,
                                "code": exc.code,
                            },
                        },
                    )
                except Exception:
                    metrics.inc("asr_errors_total")
                    logger.exception("Unhandled ASR SSE error")
                    yield _sse(
                        "error",
                        {
                            "request_id": request_id,
                            "error": {
                                "message": "An internal server error occurred",
                                "code": "stream_error",
                            },
                        },
                    )
                finally:
                    elapsed = time.monotonic() - started
                    metrics.inc("asr_processing_seconds_total", elapsed)
                    await cleanup.release()

            return StreamingResponse(
                events(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Request-Id": request_id,
                },
            )

        async def cleanup_sync() -> None:
            await gate.release()
            _unlink(temp_path)

        cleanup = DeferredCleanup(cleanup_sync)
        try:
            result = await to_thread_cancel_safe(
                resolved_backend.transcribe,
                temp_path,
                language=language,
                prompt=prompt,
                timestamps=bool(granularities or response_format in {"srt", "vtt"}),
                deferred_cleanup=cleanup,
            )
            elapsed = time.monotonic() - started
            metrics.inc("asr_processing_seconds_total", elapsed)
            if result.duration > 0:
                metrics.set("asr_last_real_time_factor", elapsed / result.duration)
            headers = {
                "X-Request-Id": request_id,
                "X-Processing-Time-Ms": str(round(elapsed * 1000)),
            }
            if response_format == "json":
                return JSONResponse({"text": result.text}, headers=headers)
            if response_format == "verbose_json":
                return JSONResponse(
                    verbose_json(result, granularities),
                    headers=headers,
                )
            payload, media_type = result_payload(result, response_format)
            return Response(payload, media_type=media_type, headers=headers)
        except Exception:
            metrics.inc("asr_errors_total")
            raise
        finally:
            await cleanup.release()

    @app.post(
        "/v1/audio/translations",
        dependencies=[Depends(require_api_auth)],
    )
    async def translations() -> None:
        raise SpeechAPIError(
            501,
            "Qwen3-ASR does not provide speech translation",
            "translations_not_supported",
            "not_implemented_error",
        )

    @app.websocket("/v1/audio/transcriptions/stream")
    async def websocket_transcription(websocket: WebSocket) -> None:
        if not websocket_authorized(websocket, token_store):
            await websocket.close(code=1008, reason="unauthorized")
            return
        if not app.state.ready or not resolved_backend.ready:
            await websocket.close(code=1013, reason="ASR model is not ready")
            return
        try:
            await gate.acquire()
        except SpeechAPIError:
            await websocket.close(code=1013, reason="ASR is busy")
            return

        async def cleanup_websocket() -> None:
            metrics.set("asr_websocket_sessions", 0)
            await gate.release()

        cleanup = DeferredCleanup(cleanup_websocket)
        try:
            await websocket.accept()
        except (Exception, asyncio.CancelledError):
            await cleanup.release()
            return
        metrics.inc("asr_websocket_sessions_total")
        metrics.set("asr_websocket_sessions", 1)
        session_id = f"session_{uuid.uuid4().hex[:24]}"
        config = TranscriptionSessionSettings()
        vad = EnergyVAD(config.vad)
        stream_state: ASRStream | None = None
        item_id: str | None = None
        previous_text = ""
        utterance_pcm: list[np.ndarray] = []
        sequence = 0
        try:
            await websocket.send_json(
                {
                    "type": "transcription_session.created",
                    "session": {
                        "id": session_id,
                        **config.model_dump(mode="json"),
                    },
                }
            )
        except (Exception, asyncio.CancelledError):
            await cleanup.release()
            return

        async def finish_current() -> None:
            nonlocal stream_state, item_id, previous_text, sequence, utterance_pcm
            if stream_state is None:
                return
            current_stream = stream_state
            current_item_id = item_id
            pcm_parts = utterance_pcm
            stream_state = None
            item_id = None
            previous_text = ""
            utterance_pcm = []
            vad.reset()
            finish_started = time.monotonic()
            update = await to_thread_cancel_safe(
                current_stream.finish,
                deferred_cleanup=cleanup,
            )
            pcm = _concatenate_pcm(pcm_parts)
            final = (
                await to_thread_cancel_safe(
                    resolved_backend.transcribe,
                    pcm,
                    language=config.language,
                    prompt=config.prompt,
                    timestamps=bool(config.timestamp_granularities),
                    deferred_cleanup=cleanup,
                )
                if pcm.size
                else ASRResult(
                    text=update.text,
                    language=update.language,
                    segments=[],
                    duration=0.0,
                )
            )
            duration_ms = round(pcm.size * 1000 / 16000)
            event: dict[str, object] = {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": current_item_id,
                "sequence": sequence + 1,
                "language": final.language,
                "transcript": final.text,
                "duration_ms": duration_ms,
            }
            if config.timestamp_granularities:
                event.update(
                    timestamp_fields(
                        final,
                        list(config.timestamp_granularities),
                    )
                )
            event["processing_ms"] = round((time.monotonic() - finish_started) * 1000)
            sequence += 1
            await websocket.send_json(event)

        async def feed_pcm(data: bytes) -> None:
            nonlocal stream_state, item_id, previous_text, sequence, utterance_pcm
            if len(data) > MAX_WEBSOCKET_AUDIO_BYTES:
                raise ValueError(
                    f"audio chunk exceeds {MAX_WEBSOCKET_AUDIO_BYTES} bytes"
                )
            if len(data) % 2:
                raise ValueError("PCM16 audio must contain complete 16-bit samples")
            pcm = pcm16_bytes_to_float(data)
            decision = vad.process(pcm)
            if decision.started:
                item_id = f"item_{uuid.uuid4().hex[:24]}"
                utterance_pcm = []
                stream_state = await to_thread_cancel_safe(
                    resolved_backend.start_stream,
                    language=config.language,
                    prompt=config.prompt,
                    deferred_cleanup=cleanup,
                )
                await websocket.send_json(
                    {
                        "type": "input_audio_buffer.speech_started",
                        "item_id": item_id,
                    }
                )
            if decision.audio.size and stream_state is not None:
                utterance_pcm.append(decision.audio.copy())
                update = await to_thread_cancel_safe(
                    stream_state.feed,
                    decision.audio,
                    deferred_cleanup=cleanup,
                )
                if update.text != previous_text:
                    sequence += 1
                    delta = _text_delta(previous_text, update.text)
                    previous_text = update.text
                    await websocket.send_json(
                        {
                            "type": "conversation.item.input_audio_transcription.delta",
                            "item_id": item_id,
                            "sequence": sequence,
                            "delta": delta,
                            "text": update.text,
                            "language": update.language,
                        }
                    )
            if decision.stopped and stream_state is not None:
                await websocket.send_json(
                    {
                        "type": "input_audio_buffer.speech_stopped",
                        "item_id": item_id,
                    }
                )
                await finish_current()

        try:
            while True:
                message = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                if message.get("type") == "websocket.disconnect":
                    break
                binary = message.get("bytes")
                if binary is not None:
                    try:
                        await feed_pcm(binary)
                    except ValueError as exc:
                        await _ws_error(
                            websocket,
                            "invalid_audio_frame",
                            str(exc),
                        )
                    continue
                raw_text = message.get("text")
                if raw_text is None:
                    continue
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    await _ws_error(
                        websocket, "invalid_json", "message must be valid JSON"
                    )
                    continue
                if not isinstance(payload, dict):
                    await _ws_error(
                        websocket,
                        "invalid_event",
                        "message must be a JSON object",
                    )
                    continue
                event_type = payload.get("type")
                try:
                    if event_type == "transcription_session.update":
                        if stream_state is not None:
                            await _ws_error(
                                websocket,
                                "session_update_while_streaming",
                                "session cannot be updated during an active utterance",
                            )
                            continue
                        parsed = SessionUpdateEvent.model_validate(payload)
                        config = parsed.session
                        vad = EnergyVAD(config.vad)
                        await websocket.send_json(
                            {
                                "type": "transcription_session.updated",
                                "session": {
                                    "id": session_id,
                                    **config.model_dump(mode="json"),
                                },
                            }
                        )
                    elif event_type == "input_audio_buffer.append":
                        parsed = AudioAppendEvent.model_validate(payload)
                        try:
                            decoded = base64.b64decode(parsed.audio, validate=True)
                        except (ValueError, binascii.Error):
                            await _ws_error(
                                websocket,
                                "invalid_audio_base64",
                                "audio must be valid base64",
                            )
                            continue
                        try:
                            await feed_pcm(decoded)
                        except ValueError as exc:
                            await _ws_error(
                                websocket,
                                "invalid_audio_frame",
                                str(exc),
                            )
                    elif event_type == "input_audio_buffer.commit":
                        ControlEvent.model_validate(payload)
                        await finish_current()
                    elif event_type == "input_audio_buffer.clear":
                        ControlEvent.model_validate(payload)
                        stream_state = None
                        item_id = None
                        previous_text = ""
                        utterance_pcm = []
                        vad.reset()
                        await websocket.send_json(
                            {"type": "input_audio_buffer.cleared"}
                        )
                    elif event_type == "session.close":
                        ControlEvent.model_validate(payload)
                        await finish_current()
                        await websocket.send_json(
                            {"type": "session.completed", "session_id": session_id}
                        )
                        await websocket.close(code=1000)
                        return
                    else:
                        await _ws_error(
                            websocket,
                            "unknown_event_type",
                            f"unsupported event type: {event_type}",
                        )
                except ValidationError as exc:
                    await _ws_error(
                        websocket,
                        "invalid_event",
                        _validation_error_message(exc),
                    )
        except TimeoutError:
            await _ws_error(
                websocket, "stream_idle_timeout", "no audio received for 30 seconds"
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)
        except WebSocketDisconnect:
            pass
        except Exception:
            metrics.inc("asr_errors_total")
            logger.exception("Unhandled ASR WebSocket error")
            with contextlib.suppress(Exception):
                await _ws_error(
                    websocket,
                    "stream_error",
                    "An internal server error occurred",
                )
                await websocket.close(code=1011)
        finally:
            await cleanup.release()

    return app


def _require_ready(app: FastAPI) -> None:
    if not app.state.ready or not app.state.backend.ready:
        raise SpeechAPIError(
            503,
            str(app.state.startup_error or "ASR model is not ready"),
            "model_not_ready",
            "service_unavailable_error",
        )


def _require_model(model: str) -> None:
    if model not in SUPPORTED_ASR_MODELS:
        raise SpeechAPIError(
            404,
            f"model not found: {model}",
            "model_not_found",
            param="model",
        )


def _warmup_enabled(fake_backend: bool) -> bool:
    default = "0" if fake_backend else "1"
    return os.getenv("QWEN3_ASR_WARMUP", default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _save_upload(
    upload: UploadFile,
    *,
    directory: Path,
    max_bytes: int,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="asr-",
        suffix=safe_suffix(upload.filename),
        dir=directory,
    )
    path = Path(raw_path)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SpeechAPIError(
                        413,
                        f"uploaded audio exceeds {max_bytes} bytes",
                        "audio_too_large",
                    )
                handle.write(chunk)
        if total == 0:
            raise SpeechAPIError(
                400,
                "uploaded audio is empty",
                "empty_audio",
                param="file",
            )
        path.chmod(0o600)
        return path
    except Exception:
        _unlink(path)
        raise


def _next_update(iterator: Iterator[StreamUpdate]) -> StreamUpdate | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _close_iterator(iterator: Iterator[StreamUpdate]) -> None:
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


def _result_from_update(update: StreamUpdate) -> ASRResult:
    segments = update.segments or []
    words = update.words or []
    duration = max(
        (float(item.get("end", 0.0)) for item in [*segments, *words]),
        default=0.0,
    )
    return ASRResult(
        text=update.text,
        language=update.language,
        segments=segments,
        words=words,
        duration=duration,
    )


def _validation_error_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first.get("loc") or ())
    message = str(first.get("msg") or "invalid event")
    return f"{location}: {message}" if location else message


def _concatenate_pcm(parts: list[np.ndarray]) -> np.ndarray:
    if not parts:
        return np.array([], dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


def _text_delta(previous: str, current: str) -> str:
    if current.startswith(previous):
        return current[len(previous) :]
    return current


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _ws_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": code,
            },
        }
    )


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


app = create_app()


if __name__ == "__main__":
    import uvicorn

    current = asr_settings()
    uvicorn.run(
        "speech.asr.app:app",
        host=current.host,
        port=current.port,
        reload=False,
    )
