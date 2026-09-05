from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.auth import is_truthy


EXPECTED_SDK_VERSION = "0.1.1"
MAX_BILLING_EVIDENCE_BYTES = 64 * 1024
_BILLING_EVIDENCE_FIELDS = {
    "schema_version",
    "runtime",
    "billing_mode",
    "profile_root",
    "schema_fingerprint",
    "provider_ids",
    "model_ids",
    "approval_mode",
    "verified_at",
}


def _csv(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if 0 < value <= maximum else default


def _path_env(name: str, default: Path) -> Path:
    configured = os.getenv(name, "").strip()
    return Path(configured).expanduser() if configured else default.expanduser()


@dataclass(frozen=True)
class BillingEvidence:
    schema_version: int
    runtime: str
    billing_mode: str
    profile_root: str
    schema_fingerprint: str
    provider_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    approval_mode: str
    verified_at: str

    @classmethod
    def load(cls, path: Path) -> BillingEvidence:
        if path.stat().st_size > MAX_BILLING_EVIDENCE_BYTES:
            raise ValueError("billing evidence file is too large")
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("billing evidence must be a JSON object")
        unknown = set(raw) - _BILLING_EVIDENCE_FIELDS
        missing = _BILLING_EVIDENCE_FIELDS - set(raw)
        if unknown or missing:
            raise ValueError("billing evidence fields do not match schema version 1")
        schema_version = raw["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("billing evidence schema_version must be an integer")
        provider_ids = raw.get("provider_ids")
        model_ids = raw.get("model_ids")
        provider_ids = _string_array(provider_ids, "provider_ids")
        model_ids = _string_array(model_ids, "model_ids")
        strings = {
            name: _required_string(raw, name)
            for name in _BILLING_EVIDENCE_FIELDS
            if name not in {"schema_version", "provider_ids", "model_ids"}
        }
        try:
            verified_at = datetime.fromisoformat(strings["verified_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("billing evidence verified_at must be an ISO 8601 timestamp") from exc
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError("billing evidence verified_at must include a UTC offset")
        return cls(
            schema_version=schema_version,
            runtime=strings["runtime"],
            billing_mode=strings["billing_mode"],
            profile_root=strings["profile_root"],
            schema_fingerprint=strings["schema_fingerprint"],
            provider_ids=provider_ids,
            model_ids=model_ids,
            approval_mode=strings["approval_mode"],
            verified_at=strings["verified_at"],
        )


@dataclass(frozen=True)
class MuseConfig:
    enabled: bool
    binary: str
    node_binary: str
    bridge_entry: Path
    profile_root: Path | None
    workspace_root: Path
    state_db: Path
    cursor_secret_file: Path
    billing_evidence_file: Path | None
    allowed_models: tuple[str, ...]
    allowed_provider_ids: tuple[str, ...]
    expected_fingerprint: str
    native_approval_mode: str
    startup_timeout_ms: int
    request_timeout_ms: int
    shutdown_timeout_ms: int
    approval_timeout_ms: int
    max_sessions: int
    debug_log: bool

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> MuseConfig:
        root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
        data_root = _path_env(
            "LOCAL_LLM_DATA_ROOT",
            Path.home() / ".local" / "share" / "local-llm",
        )
        profile = os.getenv("LOCAL_LLM_MUSE_PROFILE_ROOT", "").strip()
        evidence = os.getenv("LOCAL_LLM_MUSE_BILLING_EVIDENCE_FILE", "").strip()
        return cls(
            enabled=is_truthy(os.getenv("LOCAL_LLM_MUSE_ENABLED"), default=False),
            binary=os.getenv("LOCAL_LLM_MUSE_BINARY", "muse").strip() or "muse",
            node_binary=os.getenv("LOCAL_LLM_NODE_BINARY", "node").strip() or "node",
            bridge_entry=_path_env(
                "LOCAL_LLM_MUSE_BRIDGE_ENTRY",
                root / "bridges" / "muse" / "dist" / "src" / "main.js",
            ),
            profile_root=Path(profile).expanduser() if profile else None,
            workspace_root=_path_env(
                "LOCAL_LLM_MUSE_WORKSPACE_ROOT",
                data_root / "agent-workspaces" / "muse",
            ),
            state_db=_path_env(
                "LOCAL_LLM_AGENT_STATE_DB",
                data_root / "agent-runtime.sqlite3",
            ),
            cursor_secret_file=_path_env(
                "LOCAL_LLM_AGENT_CURSOR_SECRET_FILE",
                data_root / "agent-cursor.secret",
            ),
            billing_evidence_file=Path(evidence).expanduser() if evidence else None,
            allowed_models=_csv("LOCAL_LLM_MUSE_ALLOWED_MODELS"),
            allowed_provider_ids=_csv("LOCAL_LLM_MUSE_ALLOWED_PROVIDER_IDS"),
            expected_fingerprint=os.getenv("LOCAL_LLM_MUSE_SCHEMA_FINGERPRINT", "").strip(),
            native_approval_mode=os.getenv("LOCAL_LLM_MUSE_APPROVAL_MODE", "").strip(),
            startup_timeout_ms=_positive_int(
                "LOCAL_LLM_MUSE_STARTUP_TIMEOUT_MS", 10000, 600_000
            ),
            request_timeout_ms=_positive_int(
                "LOCAL_LLM_MUSE_REQUEST_TIMEOUT_MS", 30000, 600_000
            ),
            shutdown_timeout_ms=_positive_int(
                "LOCAL_LLM_MUSE_SHUTDOWN_TIMEOUT_MS", 30000, 600_000
            ),
            approval_timeout_ms=_positive_int(
                "LOCAL_LLM_MUSE_APPROVAL_TIMEOUT_MS", 300000, 86_400_000
            ),
            max_sessions=_positive_int("LOCAL_LLM_MUSE_MAX_SESSIONS", 2, 64),
            debug_log=is_truthy(os.getenv("LOCAL_LLM_MUSE_DEBUG_LOG"), default=False),
        )

    def resolved_binary(self) -> str | None:
        candidate = Path(self.binary).expanduser()
        if candidate.is_absolute():
            return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(self.binary)

    def resolved_node_binary(self) -> str | None:
        candidate = Path(self.node_binary).expanduser()
        if candidate.is_absolute():
            return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(self.node_binary)

    def validate_billing_evidence(self) -> tuple[BillingEvidence | None, str | None]:
        if self.billing_evidence_file is None:
            return None, "LOCAL_LLM_MUSE_BILLING_EVIDENCE_FILE is required"
        if self.billing_evidence_file.is_symlink():
            return None, "Muse billing evidence file must not be a symlink"
        if not self.billing_evidence_file.is_file():
            return None, "Muse billing evidence file does not exist"
        try:
            evidence_mode = self.billing_evidence_file.stat().st_mode
        except OSError as exc:
            return None, f"Muse billing evidence file is unavailable: {exc}"
        if evidence_mode & 0o077:
            return None, "Muse billing evidence file permissions must be 0600"
        try:
            evidence = BillingEvidence.load(self.billing_evidence_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, f"Muse billing evidence is invalid: {exc}"
        if evidence.schema_version != 1 or evidence.runtime != "muse":
            return None, "Muse billing evidence has an unsupported schema or runtime"
        if evidence.billing_mode != "subscription":
            return None, "Muse billing evidence does not confirm subscription mode"
        if self.profile_root is None:
            return None, "LOCAL_LLM_MUSE_PROFILE_ROOT is required"
        if self.profile_root.is_symlink():
            return None, "Muse profile root must not be a symlink"
        if not self.profile_root.is_dir():
            return None, "Muse profile root does not exist"
        try:
            profile_mode = self.profile_root.stat().st_mode
        except OSError as exc:
            return None, f"Muse profile root is unavailable: {exc}"
        if profile_mode & 0o077:
            return None, "Muse profile root permissions must be 0700"
        if Path(evidence.profile_root).expanduser().resolve() != self.profile_root.resolve():
            return None, "Muse billing evidence was created for another profile root"
        if not self.expected_fingerprint or evidence.schema_fingerprint != self.expected_fingerprint:
            return None, "Muse billing evidence fingerprint does not match configuration"
        if not self.allowed_provider_ids or not set(self.allowed_provider_ids).issubset(evidence.provider_ids):
            return None, "Configured Muse providers are not covered by billing evidence"
        if not self.allowed_models or not set(self.allowed_models).issubset(evidence.model_ids):
            return None, "Configured Muse models are not covered by billing evidence"
        if self.native_approval_mode not in {"promptUnmatched", "onRequest", "denyUnmatched"}:
            return None, "LOCAL_LLM_MUSE_APPROVAL_MODE must be verified and explicitly configured"
        if evidence.approval_mode != self.native_approval_mode:
            return None, "Configured Muse approval mode is not covered by billing evidence"
        if not evidence.verified_at:
            return None, "Muse billing evidence has no verification timestamp"
        return evidence, None

    def child_env(self) -> dict[str, str]:
        if self.profile_root is None:
            raise ValueError("Muse profile root is required")
        env = {
            "HOME": str(self.profile_root.resolve()),
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "TBH_CREDENTIAL_BACKEND": "file",
            "TBH_DISABLE_TELEMETRY": "1",
            "MUSE_EXPERIMENTAL_SDK_ENABLED": "on",
        }
        for name in ("LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
            value = os.getenv(name)
            if value:
                env[name] = value
        return env


def _required_string(value: dict[str, Any], name: str) -> str:
    member = value.get(name)
    if not isinstance(member, str) or not member.strip():
        raise ValueError(f"billing evidence {name} must be a non-empty string")
    return member


def _string_array(value: Any, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"billing evidence {name} must be a non-empty unique string array")
    return tuple(value)
