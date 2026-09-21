from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from agent_runtime.service import get_agent_service, shutdown_agent_service
from api.auth import require_api_auth
from core.local_inference import local_inference_enabled
from api.routes.agents import router as agents_router
from api.routes.chat import router as chat_router
from api.routes.models import router as models_router
from api.routes.responses import router as responses_router
from core.daemon import get_local_llm_daemon
from core.commandcode_provider import commandcode_status
from shared.auth import auth_status


def _is_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    daemon = None
    if local_inference_enabled():
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
                **(_local_health() if daemon is None else daemon.health()),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        yield
    finally:
        await shutdown_agent_service()
        if daemon is not None:
            daemon.shutdown()


app = FastAPI(
    title="local-llm Runtime Gateway",
    description="Local OpenAI-compatible model APIs with opt-in external agent runtimes.",
    version="0.1.0",
    lifespan=lifespan,
)

model_dependencies = [Depends(require_api_auth)]
app.include_router(models_router, dependencies=model_dependencies)
app.include_router(chat_router, dependencies=model_dependencies)
app.include_router(responses_router, dependencies=model_dependencies)
app.include_router(agents_router, dependencies=[Depends(require_api_auth)])


@app.get("/health")
def health() -> dict[str, object]:
    return _local_health()


@app.get("/status")
async def status() -> dict[str, object]:
    return {
        "service": "local-llm-api",
        "health": _local_health(),
        "auth": auth_status(),
        "api": {
            "models": "/v1/models",
            "chatCompletions": "/v1/chat/completions",
            "responses": "/v1/responses",
            "agentRuntimes": "/v1/agents/runtimes",
            "agentModels": "/v1/agents/models",
        },
        "providers": {"commandcode": commandcode_status()},
        "agents": {"runtimes": await get_agent_service().list_runtimes()},
    }


def _local_health() -> dict[str, object]:
    if not local_inference_enabled():
        return {"enabled": False, "loaded": False, "status": "disabled"}
    return {"enabled": True, **get_local_llm_daemon().health()}


if __name__ == "__main__":
    import os
    import uvicorn

    host = os.getenv("GEMMA4_API_HOST", "0.0.0.0")
    port = int(os.getenv("GEMMA4_API_PORT", "44448"))
    uvicorn.run("api.main:app", host=host, port=port, reload=False)
