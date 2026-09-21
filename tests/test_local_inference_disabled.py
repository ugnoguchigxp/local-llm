import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.auth import require_api_auth
from core.model import MLXModelManager


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/models", None),
    ("POST", "/v1/chat/completions", {"model": "default", "messages": [{"role": "user", "content": "hello"}]}),
    ("POST", "/v1/responses", {"model": "default", "input": "hello"}),
])
def test_disabled_routes_never_reach_model(monkeypatch, method, path, body):
    monkeypatch.setenv("LOCAL_INFERENCE_ENABLED", "false")
    def unexpected():
        raise AssertionError("Disabled route reached the daemon")
    monkeypatch.setattr("api.routes.chat.get_local_llm_daemon", unexpected)
    monkeypatch.setattr("api.routes.responses.get_local_llm_daemon", unexpected)
    monkeypatch.setattr("api.routes.models.get_model_manager", unexpected)
    monkeypatch.setitem(app.dependency_overrides, require_api_auth, lambda: None)
    response = TestClient(app).request(method, path, json=body)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "local_inference_disabled"


def test_direct_load_is_also_blocked(monkeypatch):
    monkeypatch.setenv("LOCAL_INFERENCE_ENABLED", "false")
    manager = MLXModelManager()
    with pytest.raises(RuntimeError, match="local_inference_disabled"):
        manager.ensure_loaded()
    assert manager.health()["loaded"] is False


def test_health_does_not_construct_daemon_when_local_inference_is_disabled(monkeypatch):
    monkeypatch.setenv("LOCAL_INFERENCE_ENABLED", "false")
    monkeypatch.setattr(
        "api.main.get_local_llm_daemon",
        lambda: (_ for _ in ()).throw(AssertionError("disabled health reached daemon")),
    )
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "loaded": False, "status": "disabled"}
