from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from api.auth import require_api_auth
from speech.common.async_utils import DeferredCleanup, to_thread_cancel_safe
from speech.common.errors import SpeechAPIError, install_error_handlers
from speech.common.gate import InferenceGate
from speech.common.metrics import Metrics
from speech.common.settings import CommonSettings, tts_settings
from speech.tts.audio import (
    CONTENT_TYPES,
    encode_buffered,
    ffmpeg_stream_args,
    float_to_pcm16,
    streaming_wav_header,
)
from speech.tts.backend import (
    AudioChunk,
    TTSBackend,
    create_tts_backend,
)
from speech.tts.schemas import SpeechRequest, VoiceDesignRequest
from speech.tts.voices import VoiceStore, safe_audio_suffix

SUPPORTED_TTS_MODELS = frozenset(
    {
        "qwen3-tts-1.7b-custom-voice",
        "qwen3-tts-1.7b",
    }
)


def create_app(
    *,
    settings: CommonSettings | None = None,
    backend: TTSBackend | None = None,
    voice_store: VoiceStore | None = None,
) -> FastAPI:
    resolved_settings = settings or tts_settings()
    resolved_backend = backend or create_tts_backend(resolved_settings)
    resolved_settings.ensure_directories()
    resolved_store = voice_store or VoiceStore(resolved_settings.data_root / "voices")
    gate = InferenceGate(
        concurrency=1,
        queue_size=resolved_settings.queue_size,
        timeout_seconds=resolved_settings.inference_timeout_seconds,
    )
    metrics = Metrics("qwen3-tts")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ready = False
        application.state.startup_error = None
        reaper_task: asyncio.Task[None] | None = None
        if resolved_settings.preload:
            try:
                await asyncio.to_thread(resolved_backend.load)
                if _warmup_enabled(resolved_settings.fake_backend):
                    await asyncio.to_thread(resolved_backend.warmup)
                application.state.ready = True
                reaper_task = asyncio.create_task(
                    _secondary_model_reaper(resolved_backend, gate)
                )
            except Exception as exc:
                application.state.startup_error = str(exc)
                if resolved_settings.fake_backend:
                    raise
        yield
        application.state.ready = False
        if reaper_task is not None:
            reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task
        await asyncio.to_thread(resolved_backend.close)

    app = FastAPI(
        title="Qwen3 TTS OpenAI-Compatible API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    app.state.startup_error = None
    app.state.backend = resolved_backend
    app.state.settings = resolved_settings
    app.state.voice_store = resolved_store
    app.state.gate = gate
    app.state.metrics = metrics
    install_error_handlers(app)

    @app.get("/live")
    async def live() -> dict[str, object]:
        return {"status": "ok", "service": "qwen3-tts"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        is_ready = bool(app.state.ready and resolved_backend.ready)
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "service": "qwen3-tts",
                "model": resolved_backend.model_info() if is_ready else None,
                "error": app.state.startup_error,
                "queue": {"active": gate.active, "waiting": gate.waiting},
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if app.state.ready else "degraded",
            "service": "qwen3-tts",
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
        info = resolved_backend.model_info()
        return {"object": "list", "data": [info]}

    @app.post("/v1/audio/speech", dependencies=[Depends(require_api_auth)])
    async def speech(request: SpeechRequest):
        _require_model(request.model)
        _require_ready(app)
        voice_profile = None
        if request.voice_id.startswith("voice_"):
            voice_profile = resolved_store.get_voice(request.voice_id)
            if not voice_profile:
                raise SpeechAPIError(
                    404,
                    f"voice profile not found: {request.voice_id}",
                    "voice_not_found",
                    param="voice",
                )
        request_id = f"tts_{uuid.uuid4().hex[:24]}"
        await gate.acquire()
        started = time.monotonic()
        metrics.inc("tts_requests_total")
        iterator: Iterator[AudioChunk] = iter(())

        async def cleanup_request() -> None:
            try:
                _close_iterator(iterator)
            finally:
                await gate.release()

        cleanup = DeferredCleanup(cleanup_request)
        try:
            iterator = resolved_backend.stream(request, voice_profile)
            first = await to_thread_cancel_safe(
                _next_chunk,
                iterator,
                deferred_cleanup=cleanup,
            )
            if first is None:
                raise SpeechAPIError(
                    500,
                    "TTS model produced no audio",
                    "empty_audio",
                    "server_error",
                )
        except asyncio.CancelledError:
            metrics.inc("tts_stream_cancellations_total")
            await cleanup.release()
            raise
        except Exception:
            metrics.inc("tts_errors_total")
            await cleanup.release()
            raise

        headers = {
            "X-Request-Id": request_id,
            "X-Model-Id": request.model,
            "X-Audio-Sample-Rate": str(first.sample_rate),
            "X-Audio-Channels": "1",
            "X-Audio-Encoding": (
                "pcm_s16le"
                if request.response_format == "pcm"
                else request.response_format
            ),
        }

        if request.response_format in {"aac", "flac"}:
            samples = int(first.audio.size)
            try:
                remaining = await to_thread_cancel_safe(
                    _collect_chunks,
                    iterator,
                    deferred_cleanup=cleanup,
                )
                samples += sum(int(chunk.audio.size) for chunk in remaining)
                payload = await asyncio.to_thread(
                    encode_buffered,
                    [first.audio, *(chunk.audio for chunk in remaining)],
                    sample_rate=first.sample_rate,
                    response_format=request.response_format,
                )
                elapsed = time.monotonic() - started
                metrics.inc("tts_generation_seconds_total", elapsed)
                audio_seconds = samples / max(1, first.sample_rate)
                metrics.inc("tts_audio_seconds_total", audio_seconds)
                if audio_seconds > 0:
                    real_time_factor = elapsed / audio_seconds
                    metrics.set("tts_last_real_time_factor", real_time_factor)
                    headers["X-Real-Time-Factor"] = f"{real_time_factor:.4f}"
                headers["X-Processing-Time-Ms"] = str(round(elapsed * 1000))
                headers["Content-Length"] = str(len(payload))
                return Response(
                    payload,
                    media_type=CONTENT_TYPES[request.response_format],
                    headers=headers,
                )
            except asyncio.CancelledError:
                metrics.inc("tts_stream_cancellations_total")
                raise
            except Exception:
                metrics.inc("tts_errors_total")
                raise
            finally:
                await cleanup.release()

        async def body() -> AsyncIterator[bytes]:
            samples = 0
            try:
                if request.response_format in {"pcm", "wav"}:
                    if request.response_format == "wav":
                        yield streaming_wav_header(first.sample_rate)
                    async for chunk in _iterate_chunks(first, iterator, cleanup):
                        samples += int(chunk.audio.size)
                        yield float_to_pcm16(chunk.audio)
                else:
                    async for encoded, count in _stream_compressed(
                        first,
                        iterator,
                        request.response_format,
                        cleanup,
                    ):
                        samples += count
                        yield encoded
            except asyncio.CancelledError:
                metrics.inc("tts_stream_cancellations_total")
                raise
            except Exception:
                metrics.inc("tts_errors_total")
                raise
            finally:
                elapsed = time.monotonic() - started
                audio_seconds = samples / max(1, first.sample_rate)
                metrics.inc("tts_generation_seconds_total", elapsed)
                metrics.inc("tts_audio_seconds_total", audio_seconds)
                if audio_seconds > 0:
                    metrics.set("tts_last_real_time_factor", elapsed / audio_seconds)
                await cleanup.release()

        return StreamingResponse(
            body(),
            media_type=CONTENT_TYPES[request.response_format],
            headers=headers,
        )

    @app.post("/v1/audio/voice_consents", dependencies=[Depends(require_api_auth)])
    async def create_consent(
        name: str = Form(...),
        language: str = Form(...),
        recording: UploadFile = File(...),
        owner: str = Form("local-user"),
        usage_scope: str = Form("local-tts"),
    ) -> dict[str, object]:
        payload = await _limited_upload(recording, max_bytes=30 * 1024 * 1024)
        return resolved_store.create_consent(
            name=name,
            language=language,
            owner=owner,
            usage_scope=usage_scope,
            recording=payload,
            suffix=safe_audio_suffix(recording.filename, recording.content_type),
        )

    @app.post("/v1/audio/voices", dependencies=[Depends(require_api_auth)])
    async def create_voice(
        name: str = Form(...),
        audio_sample: UploadFile = File(...),
        reference_text: str = Form(...),
        consent: str = Form(...),
        language: str = Form("Japanese"),
    ) -> dict[str, object]:
        payload = await _limited_upload(audio_sample, max_bytes=30 * 1024 * 1024)
        return resolved_store.create_voice(
            name=name,
            language=language,
            reference_text=reference_text,
            consent_id=consent,
            audio=payload,
            suffix=safe_audio_suffix(audio_sample.filename, audio_sample.content_type),
        )

    @app.get("/v1/audio/voices", dependencies=[Depends(require_api_auth)])
    async def list_voices() -> dict[str, object]:
        return {"object": "list", "data": resolved_store.list_voices()}

    @app.get("/v1/audio/voices/{voice_id}", dependencies=[Depends(require_api_auth)])
    async def get_voice(voice_id: str) -> dict[str, object]:
        voice = resolved_store.get_voice(voice_id, include_internal=False)
        if not voice:
            raise SpeechAPIError(404, "voice not found", "voice_not_found")
        return voice

    @app.delete("/v1/audio/voices/{voice_id}", dependencies=[Depends(require_api_auth)])
    async def delete_voice(voice_id: str) -> dict[str, object]:
        if not resolved_store.delete_voice(voice_id):
            raise SpeechAPIError(404, "voice not found", "voice_not_found")
        return {"id": voice_id, "object": "audio.voice.deleted", "deleted": True}

    @app.post("/v1/audio/voices/design", dependencies=[Depends(require_api_auth)])
    async def design_voice(request: VoiceDesignRequest) -> dict[str, object]:
        _require_ready(app)
        await gate.acquire()
        try:
            candidates = await asyncio.to_thread(
                resolved_backend.design,
                text=request.preview_text,
                description=request.description,
                language=request.language,
                candidates=request.candidates,
                seed=request.seed,
            )
            created = []
            for index, candidate in enumerate(candidates, start=1):
                wav = encode_buffered(
                    [candidate.audio],
                    sample_rate=candidate.sample_rate,
                    response_format="wav",
                )
                created.append(
                    resolved_store.create_voice(
                        name=f"{request.name} {index}",
                        language=request.language,
                        reference_text=request.preview_text,
                        consent_id=None,
                        audio=wav,
                        suffix=".wav",
                        source="voice_design",
                        metadata={"description": request.description},
                    )
                )
            return {"object": "list", "data": created}
        finally:
            await gate.release()

    return app


def _require_ready(app: FastAPI) -> None:
    if not app.state.ready or not app.state.backend.ready:
        raise SpeechAPIError(
            503,
            str(app.state.startup_error or "TTS model is not ready"),
            "model_not_ready",
            "service_unavailable_error",
        )


def _require_model(model: str) -> None:
    if model not in SUPPORTED_TTS_MODELS:
        raise SpeechAPIError(
            404,
            f"model not found: {model}",
            "model_not_found",
            param="model",
        )


def _warmup_enabled(fake_backend: bool) -> bool:
    default = "0" if fake_backend else "1"
    return os.getenv("QWEN3_TTS_WARMUP", default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _secondary_model_reaper(
    backend: TTSBackend,
    gate: InferenceGate,
) -> None:
    max_idle = max(
        0.0,
        float(os.getenv("QWEN3_TTS_SECONDARY_KEEPALIVE_SECONDS", "600")),
    )
    interval = max(5.0, min(60.0, max_idle / 2.0 if max_idle else 5.0))
    while True:
        await asyncio.sleep(interval)
        if gate.active == 0:
            await asyncio.to_thread(backend.reap_secondary, max_idle)


def _next_chunk(iterator: Iterator[AudioChunk]) -> AudioChunk | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _collect_chunks(iterator: Iterator[AudioChunk]) -> list[AudioChunk]:
    return list(iterator)


def _close_iterator(iterator: Iterator[AudioChunk]) -> None:
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


async def _iterate_chunks(
    first: AudioChunk,
    iterator: Iterator[AudioChunk],
    cleanup: DeferredCleanup,
) -> AsyncIterator[AudioChunk]:
    yield first
    while True:
        chunk = await _next_chunk_cancel_safe(iterator, cleanup)
        if chunk is None:
            return
        yield chunk


async def _next_chunk_cancel_safe(
    iterator: Iterator[AudioChunk],
    cleanup: DeferredCleanup,
) -> AudioChunk | None:
    return await to_thread_cancel_safe(
        _next_chunk,
        iterator,
        deferred_cleanup=cleanup,
    )


async def _stream_compressed(
    first: AudioChunk,
    iterator: Iterator[AudioChunk],
    response_format: str,
    cleanup: DeferredCleanup,
) -> AsyncIterator[tuple[bytes, int]]:
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_stream_args(first.sample_rate, response_format),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    samples_written = 0

    async def pump() -> None:
        nonlocal samples_written
        try:
            async for chunk in _iterate_chunks(first, iterator, cleanup):
                samples_written += int(chunk.audio.size)
                process.stdin.write(float_to_pcm16(chunk.audio))
                await process.stdin.drain()
        finally:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()

    pump_task = asyncio.create_task(pump())
    reported_samples = 0
    try:
        while True:
            data = await process.stdout.read(16384)
            if not data:
                break
            delta = max(0, samples_written - reported_samples)
            reported_samples = samples_written
            yield data, delta
        await pump_task
        return_code = await process.wait()
        if return_code != 0:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            raise RuntimeError(
                f"ffmpeg exited with {return_code}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
    finally:
        encoder_cleanup = asyncio.create_task(_stop_encoder(process, pump_task))
        try:
            await asyncio.shield(encoder_cleanup)
        except asyncio.CancelledError:
            pass


async def _stop_encoder(
    process: asyncio.subprocess.Process,
    pump_task: asyncio.Task[None],
) -> None:
    if not pump_task.done():
        pump_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await pump_task
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()


async def _limited_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SpeechAPIError(
            413,
            f"uploaded audio exceeds {max_bytes} bytes",
            "audio_too_large",
        )
    return data


app = create_app()


if __name__ == "__main__":
    import uvicorn

    current = tts_settings()
    uvicorn.run(
        "speech.tts.app:app",
        host=current.host,
        port=current.port,
        reload=False,
    )
