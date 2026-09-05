from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from agent_runtime.errors import AgentRuntimeError
from api.auth import require_api_auth
from api.routes import agents as agents_module


class FakeAgentService:
    async def list_runtimes(self):
        return [{"id": "muse", "status": "ready", "billing_mode": "subscription"}]

    async def list_models(self, runtime):
        assert runtime == "muse"
        return [{"id": "muse/model-a", "object": "agent.model", "runtime": "muse"}]

    async def create_session(self, **kwargs):
        return {
            "id": "ags_abc",
            "runtime": kwargs["runtime_id"],
            "model": kwargs["public_model_id"],
            "status": "idle",
        }

    def get_session(self, session_id):
        if session_id == "missing":
            raise AgentRuntimeError("agent_session_not_found", "not found", 404)
        return {"id": session_id, "runtime": "muse", "status": "idle"}


def build_client(monkeypatch, *, auth=False, service=None):
    fake = service or FakeAgentService()
    monkeypatch.setattr(agents_module, "get_agent_service", lambda: fake)
    app = FastAPI()
    dependencies = [Depends(require_api_auth)] if auth else None
    app.include_router(agents_module.router, dependencies=dependencies)
    return TestClient(app)


def test_agent_catalog_and_session_creation_contract(monkeypatch):
    client = build_client(monkeypatch)

    runtimes = client.get("/v1/agents/runtimes")
    models = client.get("/v1/agents/models?runtime=muse")
    session = client.post(
        "/v1/agents/sessions",
        headers={"Idempotency-Key": "create-1"},
        json={
            "runtime": "muse",
            "model": "muse/model-a",
            "approval_policy": "strict",
            "workspace": {"mode": "isolated"},
        },
    )

    assert runtimes.status_code == 200
    assert runtimes.json()["data"][0]["id"] == "muse"
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "muse/model-a"
    assert session.status_code == 201
    assert session.json()["id"] == "ags_abc"


def test_agent_mutation_requires_idempotency_key(monkeypatch):
    client = build_client(monkeypatch)

    response = client.post(
        "/v1/agents/sessions",
        json={"runtime": "muse", "model": "muse/model-a"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


def test_agent_error_uses_agent_error_envelope(monkeypatch):
    client = build_client(monkeypatch)

    response = client.get("/v1/agents/sessions/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_session_not_found"
    assert response.json()["error"]["request_id"].startswith("req_")


def test_agent_routes_use_existing_bearer_auth(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LOCAL_LLM_ACCESS_TOKEN", "test-token")
    client = build_client(monkeypatch, auth=True)

    denied = client.get("/v1/agents/runtimes")
    allowed = client.get(
        "/v1/agents/runtimes",
        headers={"Authorization": "Bearer test-token"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_agent_model_response_reports_status_after_lazy_preflight(monkeypatch):
    class TransitioningService(FakeAgentService):
        def __init__(self):
            self.ready = False

        async def list_runtimes(self):
            return [{"id": "muse", "status": "ready" if self.ready else "configured"}]

        async def list_models(self, runtime):
            self.ready = True
            return await super().list_models(runtime)

    client = build_client(monkeypatch, service=TransitioningService())

    response = client.get("/v1/agents/models?runtime=muse")

    assert response.status_code == 200
    assert response.json()["runtime"]["status"] == "ready"
