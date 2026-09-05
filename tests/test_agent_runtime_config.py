from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime.muse.config import MuseConfig
from api.agent_schemas import AnswerUserInputRequest, StartAgentTurnRequest


def _configure(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    evidence = tmp_path / "billing.json"
    evidence.touch(mode=0o600)
    monkeypatch.setenv("LOCAL_LLM_MUSE_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_MUSE_PROFILE_ROOT", str(profile))
    monkeypatch.setenv("LOCAL_LLM_MUSE_BILLING_EVIDENCE_FILE", str(evidence))
    monkeypatch.setenv("LOCAL_LLM_MUSE_ALLOWED_PROVIDER_IDS", "provider-a")
    monkeypatch.setenv("LOCAL_LLM_MUSE_ALLOWED_MODELS", "model-a")
    monkeypatch.setenv("LOCAL_LLM_MUSE_SCHEMA_FINGERPRINT", "sha256:test")
    monkeypatch.setenv("LOCAL_LLM_MUSE_APPROVAL_MODE", "onRequest")
    return profile, evidence


def test_billing_evidence_must_match_profile_fingerprint_and_allowlists(monkeypatch, tmp_path):
    profile, evidence = _configure(monkeypatch, tmp_path)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime": "muse",
                "billing_mode": "subscription",
                "profile_root": str(profile),
                "schema_fingerprint": "sha256:test",
                "provider_ids": ["provider-a"],
                "model_ids": ["model-a"],
                "approval_mode": "onRequest",
                "verified_at": "2026-09-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    config = MuseConfig.from_env(repo_root=tmp_path)

    loaded, error = config.validate_billing_evidence()

    assert error is None
    assert loaded is not None and loaded.billing_mode == "subscription"


def test_billing_evidence_fails_closed_when_approval_mode_is_unverified(monkeypatch, tmp_path):
    profile, evidence = _configure(monkeypatch, tmp_path)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime": "muse",
                "billing_mode": "subscription",
                "profile_root": str(profile),
                "schema_fingerprint": "sha256:test",
                "provider_ids": ["provider-a"],
                "model_ids": ["model-a"],
                "approval_mode": "promptUnmatched",
                "verified_at": "2026-09-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    config = MuseConfig.from_env(repo_root=tmp_path)

    loaded, error = config.validate_billing_evidence()

    assert loaded is None
    assert error == "Configured Muse approval mode is not covered by billing evidence"


def test_child_environment_does_not_inherit_payg_keys(monkeypatch, tmp_path):
    profile, _evidence = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    config = MuseConfig.from_env(repo_root=tmp_path)

    child = config.child_env()

    assert child["HOME"] == str(profile.resolve())
    assert "OPENAI_API_KEY" not in child
    assert "ANTHROPIC_API_KEY" not in child


def test_approval_timeout_is_positive_and_defaults_safely(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_MUSE_APPROVAL_TIMEOUT_MS", "0")

    config = MuseConfig.from_env(repo_root=tmp_path)

    assert config.approval_timeout_ms == 300000


def test_runtime_numeric_limits_reject_unsafe_extremes(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_MUSE_APPROVAL_TIMEOUT_MS", "999999999999")
    monkeypatch.setenv("LOCAL_LLM_MUSE_MAX_SESSIONS", "999")

    config = MuseConfig.from_env(repo_root=tmp_path)

    assert config.approval_timeout_ms == 300000
    assert config.max_sessions == 2


def test_blank_common_state_paths_use_data_root_defaults(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_LLM_AGENT_STATE_DB", "")
    monkeypatch.setenv("LOCAL_LLM_AGENT_CURSOR_SECRET_FILE", "")

    config = MuseConfig.from_env(repo_root=tmp_path)

    assert config.state_db == tmp_path / "data" / "agent-runtime.sqlite3"
    assert config.cursor_secret_file == tmp_path / "data" / "agent-cursor.secret"


def test_billing_evidence_rejects_coerced_types_and_invalid_timestamp(monkeypatch, tmp_path):
    profile, evidence = _configure(monkeypatch, tmp_path)
    payload = {
        "schema_version": "1",
        "runtime": "muse",
        "billing_mode": "subscription",
        "profile_root": str(profile),
        "schema_fingerprint": "sha256:test",
        "provider_ids": ["provider-a"],
        "model_ids": ["model-a"],
        "approval_mode": "onRequest",
        "verified_at": "not-a-timestamp",
    }
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    loaded, error = MuseConfig.from_env(repo_root=tmp_path).validate_billing_evidence()

    assert loaded is None
    assert error is not None and "invalid" in error


def test_turn_input_has_aggregate_wire_safe_size_limit():
    with pytest.raises(ValidationError, match="aggregate input is too large"):
        StartAgentTurnRequest(
            input=[
                {"type": "text", "text": "x" * 600_000},
                {"type": "text", "text": "y" * 600_000},
            ]
        )


def test_user_input_answers_reject_empty_values_and_duplicate_questions():
    with pytest.raises(ValidationError):
        AnswerUserInputRequest(
            answers=[{"question_id": "q1", "selected_labels": []}]
        )
    with pytest.raises(ValidationError, match="duplicate question ids"):
        AnswerUserInputRequest(
            answers=[
                {"question_id": "q1", "selected_label": "one"},
                {"question_id": "q1", "free_text": "two"},
            ]
        )
