from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_runtime.grok.config import AUTH_METHOD, GrokConfig


def _configure(monkeypatch, tmp_path):
    profile = tmp_path / "grok-home"
    profile.mkdir(mode=0o700)
    evidence = tmp_path / "evidence.json"
    monkeypatch.setenv("LOCAL_LLM_GROK_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_GROK_BINARY", "/bin/echo")
    monkeypatch.setenv("LOCAL_LLM_GROK_HOME", str(profile))
    monkeypatch.setenv("LOCAL_LLM_GROK_BILLING_EVIDENCE_FILE", str(evidence))
    monkeypatch.setenv("LOCAL_LLM_GROK_ALLOWED_MODELS", "grok-4.6")
    monkeypatch.setenv("LOCAL_LLM_GROK_EXPECTED_VERSION", "1.0.13")
    payload = {
        "schema_version": 1,
        "runtime": "grok",
        "billing_mode": "subscription",
        "billing_assurance": "operator_attested",
        "profile_root": str(profile),
        "auth_method": AUTH_METHOD,
        "account_fingerprint": "unavailable",
        "plan": "operator-confirmed",
        "extra_usage_credits": "zero",
        "auto_top_up": "disabled",
        "grok_version": "1.0.13",
        "acp_protocol_version": 1,
        "model_ids": ["grok-4.6"],
        "sandbox_profile": "workspace",
        "verified_at": "2026-09-06T00:00:00Z",
        "expires_at": "2026-09-13T00:00:00Z",
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    evidence.chmod(0o600)
    return profile, evidence, payload


def test_default_is_disabled_and_workspace_sandbox(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_GROK_ENABLED", raising=False)
    config = GrokConfig.from_env()

    assert config.enabled is False
    assert config.sandbox_profile == "workspace"
    assert config.allow_web_search is False


def test_billing_evidence_is_expiring_and_fail_closed(monkeypatch, tmp_path):
    _profile, evidence, payload = _configure(monkeypatch, tmp_path)
    config = GrokConfig.from_env()

    loaded, error = config.validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    assert error is None and loaded is not None

    payload["auto_top_up"] = "enabled"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    loaded, error = config.validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    assert loaded is None and error == "Grok additional usage spending is not disabled"

    payload["auto_top_up"] = "disabled"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    loaded, error = config.validate_billing_evidence(
        now=datetime(2026, 9, 14, tzinfo=timezone.utc)
    )
    assert loaded is None and error == "Grok billing evidence has expired"

    duplicate = json.dumps(payload).replace(
        '"runtime": "grok"',
        '"runtime": "grok", "runtime": "grok"',
    )
    evidence.write_text(duplicate, encoding="utf-8")
    loaded, error = config.validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    assert loaded is None and "duplicate JSON key" in (error or "")


def test_billing_evidence_rejects_future_and_overlong_attestations(monkeypatch, tmp_path):
    _profile, evidence, payload = _configure(monkeypatch, tmp_path)
    payload["verified_at"] = "2026-09-07T01:00:00Z"
    payload["expires_at"] = "2026-09-14T01:00:00Z"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    loaded, error = GrokConfig.from_env().validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    assert loaded is None and "future" in (error or "")

    payload["verified_at"] = "2026-09-06T00:00:00Z"
    payload["expires_at"] = "2026-09-20T00:00:00Z"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    loaded, error = GrokConfig.from_env().validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )
    assert loaded is None and "eight days" in (error or "")


def test_local_evidence_cannot_claim_provider_verified_assurance(monkeypatch, tmp_path):
    _profile, evidence, payload = _configure(monkeypatch, tmp_path)
    payload["billing_assurance"] = "provider_verified"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    loaded, error = GrokConfig.from_env().validate_billing_evidence(
        now=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )

    assert loaded is None
    assert "only supports operator_attested" in (error or "")


def test_child_environment_drops_cloud_keys_and_command_is_safe(monkeypatch, tmp_path):
    profile, _evidence, _payload = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/untrusted-ca.pem")
    monkeypatch.setenv("TMPDIR", "/tmp/untrusted-grok-tmp")
    config = GrokConfig.from_env()

    child = config.child_env()
    command = config.command(workspace_root=str(tmp_path), model_id="grok-4.6")

    assert child["HOME"] == str(profile.resolve())
    assert child["GROK_HOME"] == str(profile.resolve() / ".grok")
    assert not {"XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}.intersection(child)
    assert "SSL_CERT_FILE" not in child and "TMPDIR" not in child
    assert command[0] == "/bin/echo"
    assert command[command.index("--sandbox") + 1] == "workspace"
    assert "--permission-mode" in command and "default" in command
    assert "--no-subagents" in command and "--disable-web-search" in command
    assert "--no-memory" in command and "--no-plan" in command


def test_enabled_runtime_requires_absolute_security_paths(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_GROK_BINARY", "grok")
    assert GrokConfig.from_env().validate_static() == (
        "LOCAL_LLM_GROK_BINARY must be an absolute path"
    )

    monkeypatch.setenv("LOCAL_LLM_GROK_BINARY", "/bin/echo")
    monkeypatch.setenv("LOCAL_LLM_GROK_HOME", "relative-profile")
    assert GrokConfig.from_env().validate_static() == (
        "LOCAL_LLM_GROK_HOME must be an absolute path"
    )


def test_profile_permissions_and_unsafe_options_are_rejected(monkeypatch, tmp_path):
    profile, _evidence, _payload = _configure(monkeypatch, tmp_path)
    profile.chmod(0o755)
    config = GrokConfig.from_env()
    assert config.validate_static() == "Grok profile root permissions must be 0700"

    profile.chmod(0o700)
    monkeypatch.setenv("LOCAL_LLM_GROK_ALLOW_WEB_SEARCH", "true")
    assert "unsupported" in (GrokConfig.from_env().validate_static() or "")

    monkeypatch.setenv("LOCAL_LLM_GROK_ALLOW_WEB_SEARCH", "false")
    monkeypatch.setenv("LOCAL_LLM_GROK_ALLOW_PROJECT_EXTENSIONS", "true")
    assert "unsupported" in (GrokConfig.from_env().validate_static() or "")


def test_profile_and_evidence_modes_are_exact(monkeypatch, tmp_path):
    profile, evidence, _payload = _configure(monkeypatch, tmp_path)
    profile.chmod(0o600)
    assert GrokConfig.from_env().validate_static() == (
        "Grok profile root permissions must be 0700"
    )

    profile.chmod(0o700)
    evidence.chmod(0o400)
    loaded, error = GrokConfig.from_env().validate_billing_evidence()
    assert loaded is None
    assert error == "Grok billing evidence file permissions must be 0600"
