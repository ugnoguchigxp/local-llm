from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_runtime.grok.json_utils import loads_strict
from shared.auth import is_truthy


MAX_BILLING_EVIDENCE_BYTES = 64 * 1024
DEFAULT_ACP_PROTOCOL_VERSION = 1
DEFAULT_SANDBOX_PROFILE = "workspace"
AUTH_METHOD = "grok.com"
DEFAULT_CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
_BILLING_EVIDENCE_FIELDS = {
    "schema_version",
    "runtime",
    "billing_mode",
    "billing_assurance",
    "profile_root",
    "auth_method",
    "account_fingerprint",
    "plan",
    "extra_usage_credits",
    "auto_top_up",
    "grok_version",
    "acp_protocol_version",
    "model_ids",
    "sandbox_profile",
    "verified_at",
    "expires_at",
}


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value.strip() for value in os.getenv(name, "").split(",") if value.strip()
        )
    )


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if 0 < value <= maximum else default


def _path_env(name: str, default: Path) -> Path:
    configured = os.getenv(name, "").strip()
    return Path(configured).expanduser() if configured else default.expanduser()


def _required_string(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"billing evidence {name} must be a non-empty string")
    return value


def _timestamp(value: str, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"billing evidence {name} must be an ISO 8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"billing evidence {name} must include a UTC offset")
    return result


@dataclass(frozen=True)
class GrokBillingEvidence:
    schema_version: int
    runtime: str
    billing_mode: str
    billing_assurance: str
    profile_root: str
    auth_method: str
    account_fingerprint: str
    plan: str
    extra_usage_credits: str
    auto_top_up: str
    grok_version: str
    acp_protocol_version: int
    model_ids: tuple[str, ...]
    sandbox_profile: str
    verified_at: str
    expires_at: str

    @classmethod
    def load(cls, path: Path) -> GrokBillingEvidence:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError("billing evidence must be a readable regular file") from exc
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("billing evidence must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("billing evidence permissions must be 0600")
            if metadata.st_uid != os.getuid():
                raise ValueError("billing evidence must be owned by the current user")
            encoded = handle.read(MAX_BILLING_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_BILLING_EVIDENCE_BYTES:
            raise ValueError("billing evidence file is too large")
        try:
            raw: Any = loads_strict(encoded.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("billing evidence must be UTF-8 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("billing evidence must be a JSON object")
        if set(raw) != _BILLING_EVIDENCE_FIELDS:
            raise ValueError("billing evidence fields do not match schema version 1")
        schema_version = raw["schema_version"]
        protocol_version = raw["acp_protocol_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("billing evidence schema_version must be an integer")
        if not isinstance(protocol_version, int) or isinstance(protocol_version, bool):
            raise ValueError("billing evidence acp_protocol_version must be an integer")
        model_ids = raw["model_ids"]
        if not isinstance(model_ids, list) or not model_ids or any(
            not isinstance(item, str) or not item.strip() for item in model_ids
        ):
            raise ValueError("billing evidence model_ids must be a non-empty string array")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("billing evidence model_ids must not contain duplicates")
        strings = {
            name: _required_string(raw, name)
            for name in _BILLING_EVIDENCE_FIELDS
            if name not in {"schema_version", "acp_protocol_version", "model_ids"}
        }
        _timestamp(strings["verified_at"], "verified_at")
        _timestamp(strings["expires_at"], "expires_at")
        return cls(
            schema_version=schema_version,
            acp_protocol_version=protocol_version,
            model_ids=tuple(model_ids),
            **strings,
        )

    @property
    def expires(self) -> datetime:
        return _timestamp(self.expires_at, "expires_at")

    @property
    def verified(self) -> datetime:
        return _timestamp(self.verified_at, "verified_at")


@dataclass(frozen=True)
class GrokConfig:
    enabled: bool
    binary: str
    profile_root: Path | None
    workspace_root: Path
    state_db: Path
    cursor_secret_file: Path
    billing_evidence_file: Path | None
    allowed_models: tuple[str, ...]
    expected_version: str
    acp_protocol_version: int
    sandbox_profile: str
    allow_web_search: bool
    allow_project_extensions: bool
    startup_timeout_ms: int
    request_timeout_ms: int
    turn_timeout_ms: int
    shutdown_timeout_ms: int
    approval_timeout_ms: int
    max_sessions: int
    max_frame_bytes: int
    debug_log: bool

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> GrokConfig:
        del repo_root
        data_root = _path_env(
            "LOCAL_LLM_DATA_ROOT",
            Path.home() / ".local" / "share" / "local-llm",
        )
        profile = os.getenv("LOCAL_LLM_GROK_HOME", "").strip()
        evidence = os.getenv("LOCAL_LLM_GROK_BILLING_EVIDENCE_FILE", "").strip()
        try:
            protocol_version = int(
                os.getenv("LOCAL_LLM_GROK_ACP_PROTOCOL_VERSION", str(DEFAULT_ACP_PROTOCOL_VERSION))
            )
        except ValueError:
            protocol_version = DEFAULT_ACP_PROTOCOL_VERSION
        if protocol_version < 1 or protocol_version > 2**31 - 1:
            protocol_version = DEFAULT_ACP_PROTOCOL_VERSION
        return cls(
            enabled=is_truthy(os.getenv("LOCAL_LLM_GROK_ENABLED"), default=False),
            binary=os.getenv("LOCAL_LLM_GROK_BINARY", "grok").strip() or "grok",
            profile_root=Path(profile).expanduser() if profile else None,
            workspace_root=_path_env(
                "LOCAL_LLM_GROK_WORKSPACE_ROOT",
                data_root / "agent-workspaces" / "grok",
            ),
            state_db=_path_env("LOCAL_LLM_AGENT_STATE_DB", data_root / "agent-runtime.sqlite3"),
            cursor_secret_file=_path_env(
                "LOCAL_LLM_AGENT_CURSOR_SECRET_FILE",
                data_root / "agent-cursor.secret",
            ),
            billing_evidence_file=Path(evidence).expanduser() if evidence else None,
            allowed_models=_csv("LOCAL_LLM_GROK_ALLOWED_MODELS"),
            expected_version=os.getenv("LOCAL_LLM_GROK_EXPECTED_VERSION", "").strip(),
            acp_protocol_version=protocol_version,
            sandbox_profile=os.getenv(
                "LOCAL_LLM_GROK_SANDBOX", DEFAULT_SANDBOX_PROFILE
            ).strip()
            or DEFAULT_SANDBOX_PROFILE,
            allow_web_search=is_truthy(
                os.getenv("LOCAL_LLM_GROK_ALLOW_WEB_SEARCH"), default=False
            ),
            allow_project_extensions=is_truthy(
                os.getenv("LOCAL_LLM_GROK_ALLOW_PROJECT_EXTENSIONS"), default=False
            ),
            startup_timeout_ms=_positive_int(
                "LOCAL_LLM_GROK_STARTUP_TIMEOUT_MS", 10_000, 600_000
            ),
            request_timeout_ms=_positive_int(
                "LOCAL_LLM_GROK_REQUEST_TIMEOUT_MS", 30_000, 600_000
            ),
            turn_timeout_ms=_positive_int(
                "LOCAL_LLM_GROK_TURN_TIMEOUT_MS", 900_000, 86_400_000
            ),
            shutdown_timeout_ms=_positive_int(
                "LOCAL_LLM_GROK_SHUTDOWN_TIMEOUT_MS", 30_000, 600_000
            ),
            approval_timeout_ms=_positive_int(
                "LOCAL_LLM_GROK_APPROVAL_TIMEOUT_MS", 300_000, 86_400_000
            ),
            max_sessions=_positive_int("LOCAL_LLM_GROK_MAX_SESSIONS", 2, 64),
            max_frame_bytes=_positive_int(
                "LOCAL_LLM_GROK_MAX_FRAME_BYTES", 4 * 1024 * 1024, 32 * 1024 * 1024
            ),
            debug_log=is_truthy(os.getenv("LOCAL_LLM_GROK_DEBUG_LOG"), default=False),
        )

    def resolved_binary(self) -> str | None:
        candidate = Path(self.binary).expanduser()
        if candidate.is_absolute():
            return (
                str(candidate.resolve())
                if candidate.is_file() and os.access(candidate, os.X_OK)
                else None
            )
        return shutil.which(self.binary)

    def validate_static(self) -> str | None:
        if not Path(self.binary).expanduser().is_absolute():
            return "LOCAL_LLM_GROK_BINARY must be an absolute path"
        if self.resolved_binary() is None:
            return "Grok binary was not found"
        if self.profile_root is None:
            return "LOCAL_LLM_GROK_HOME is required"
        if not self.profile_root.is_absolute():
            return "LOCAL_LLM_GROK_HOME must be an absolute path"
        if self.profile_root.is_symlink() or not self.profile_root.is_dir():
            return "Grok profile root must be an existing non-symlink directory"
        try:
            metadata = self.profile_root.stat()
        except OSError as exc:
            return f"Grok profile root is unavailable: {exc}"
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            return "Grok profile root permissions must be 0700"
        if metadata.st_uid != os.getuid():
            return "Grok profile root must be owned by the current user"
        if not self.allowed_models:
            return "LOCAL_LLM_GROK_ALLOWED_MODELS is required"
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model) is None
            for model in self.allowed_models
        ):
            return "LOCAL_LLM_GROK_ALLOWED_MODELS contains an invalid model id"
        if not self.expected_version:
            return "LOCAL_LLM_GROK_EXPECTED_VERSION is required"
        if self.sandbox_profile != DEFAULT_SANDBOX_PROFILE:
            return "LOCAL_LLM_GROK_SANDBOX must be workspace for the verified release"
        if self.allow_web_search:
            return "Web search is unsupported by the verified Grok Runtime"
        if self.allow_project_extensions:
            return "Project extensions are unsupported by the verified Grok Runtime"
        return None

    def validate_billing_evidence(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[GrokBillingEvidence | None, str | None]:
        path = self.billing_evidence_file
        if path is None:
            return None, "LOCAL_LLM_GROK_BILLING_EVIDENCE_FILE is required"
        if not path.is_absolute():
            return None, "Grok billing evidence path must be absolute"
        if path.is_symlink() or not path.is_file():
            return None, "Grok billing evidence must be an existing non-symlink file"
        try:
            metadata = path.stat()
        except OSError as exc:
            return None, f"Grok billing evidence is unavailable: {exc}"
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            return None, "Grok billing evidence file permissions must be 0600"
        try:
            evidence = GrokBillingEvidence.load(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, f"Grok billing evidence is invalid: {exc}"
        if evidence.schema_version != 1 or evidence.runtime != "grok":
            return None, "Grok billing evidence has an unsupported schema or runtime"
        if evidence.billing_mode != "subscription":
            return None, "Grok billing evidence does not confirm subscription mode"
        if evidence.billing_assurance != "operator_attested":
            return None, (
                "Grok billing evidence schema version 1 only supports "
                "operator_attested assurance"
            )
        if evidence.auth_method != AUTH_METHOD:
            return None, "Grok billing evidence does not cover browser subscription login"
        if evidence.extra_usage_credits != "zero" or evidence.auto_top_up != "disabled":
            return None, "Grok additional usage spending is not disabled"
        if self.profile_root is None or (
            Path(evidence.profile_root).expanduser().resolve() != self.profile_root.resolve()
        ):
            return None, "Grok billing evidence was created for another profile root"
        if not Path(evidence.profile_root).expanduser().is_absolute():
            return None, "Grok billing evidence profile root must be absolute"
        if evidence.grok_version != self.expected_version:
            return None, "Grok billing evidence CLI version does not match configuration"
        if evidence.acp_protocol_version != self.acp_protocol_version:
            return None, "Grok billing evidence ACP version does not match configuration"
        if evidence.sandbox_profile != self.sandbox_profile:
            return None, "Grok billing evidence sandbox does not match configuration"
        if not set(self.allowed_models).issubset(evidence.model_ids):
            return None, "Configured Grok models are not covered by billing evidence"
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None or clock.utcoffset() is None:
            clock = clock.replace(tzinfo=timezone.utc)
        if evidence.expires <= clock:
            return None, "Grok billing evidence has expired"
        if evidence.verified > clock + timedelta(minutes=5):
            return None, "Grok billing evidence verification time is in the future"
        if evidence.expires <= evidence.verified:
            return None, "Grok billing evidence expiry must follow verification"
        if evidence.expires - evidence.verified > timedelta(days=8):
            return None, "Grok billing evidence validity must not exceed eight days"
        return evidence, None

    def child_env(self) -> dict[str, str]:
        if self.profile_root is None:
            raise ValueError("Grok profile root is required")
        grok_home = self.profile_root.resolve() / ".grok"
        env = {
            "HOME": str(self.profile_root.resolve()),
            "GROK_HOME": str(grok_home),
            "PATH": DEFAULT_CHILD_PATH,
        }
        for name in ("LANG", "LC_ALL"):
            value = os.getenv(name)
            if value:
                env[name] = value
        return env

    def command(self, *, workspace_root: str, model_id: str) -> list[str]:
        binary = self.resolved_binary()
        if binary is None:
            raise ValueError("Grok binary is unavailable")
        command = [
            binary,
            "--cwd",
            str(Path(workspace_root).resolve()),
            "--no-auto-update",
            "--sandbox",
            self.sandbox_profile,
            "--permission-mode",
            "default",
            "--no-subagents",
            "--no-memory",
            "--no-plan",
        ]
        if not self.allow_web_search:
            command.append("--disable-web-search")
        command.extend(["--model", model_id, "agent", "stdio"])
        return command
