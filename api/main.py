from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from api.auth import require_api_auth
from api.routes.chat import router as chat_router
from api.routes.models import router as models_router
from core.daemon import get_local_llm_daemon
from shared.auth import auth_status


def _is_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    daemon = get_local_llm_daemon()
    if _is_truthy(os.getenv("LOCAL_LLM_DAEMON_PRELOAD"), default=True):
        try:
            daemon.preload()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "local_llm_daemon.preload_failed",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(
        json.dumps(
            {
                "event": "local_llm_daemon.ready",
                **daemon.health(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        yield
    finally:
        daemon.shutdown()


app = FastAPI(
    title="Gemma 4 OpenAI-Compatible API",
    description="Local MLX Gemma 4 served with OpenAI-compatible endpoints.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(models_router, dependencies=[Depends(require_api_auth)])
app.include_router(chat_router, dependencies=[Depends(require_api_auth)])


@app.get("/health")
def health() -> dict[str, object]:
    return get_local_llm_daemon().health()


@app.get("/status")
def status() -> dict[str, object]:
    return {
        "service": "local-llm-api",
        "health": get_local_llm_daemon().health(),
        "auth": auth_status(),
        "api": {
            "models": "/v1/models",
            "chatCompletions": "/v1/chat/completions",
        },
    }


if __name__ == "__main__":
    import os
    import uvicorn

    host = os.getenv("GEMMA4_API_HOST", "0.0.0.0")
    port = int(os.getenv("GEMMA4_API_PORT", "44448"))
    uvicorn.run("api.main:app", host=host, port=port, reload=False)
