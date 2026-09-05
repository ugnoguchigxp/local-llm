from __future__ import annotations

import os
from pathlib import Path

from agent_runtime.errors import AgentRuntimeError


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self._configured_root = Path(os.path.abspath(root.expanduser()))
        self.root = self._configured_root.resolve()

    def create_isolated(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        if self._configured_root.is_symlink():
            self._forbidden("The configured workspace root must not be a symlink.")
        self._configured_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._configured_root.resolve() != self.root:
            self._forbidden("The configured workspace root changed unexpectedly.")
        os.chmod(self.root, 0o700)
        candidate = self.root / session_id
        if candidate.is_symlink():
            self._forbidden("Symlink workspaces are not allowed.")
        if candidate.exists() and not candidate.is_dir():
            self._forbidden("The workspace path is not a directory.")
        candidate.mkdir(mode=0o700, exist_ok=True)
        if candidate.is_symlink():
            self._forbidden("Symlink workspaces are not allowed.")
        if candidate.resolve() != candidate:
            self._forbidden("The workspace path escaped the configured root.")
        os.chmod(candidate, 0o700)
        return candidate

    def validate_isolated(self, session_id: str, workspace_path: str | Path) -> Path:
        self._validate_session_id(session_id)
        if self._configured_root.is_symlink() or self._configured_root.resolve() != self.root:
            self._forbidden("The configured workspace root changed unexpectedly.")
        expected = self.root / session_id
        supplied = Path(os.path.abspath(Path(workspace_path).expanduser()))
        if supplied != expected or expected.is_symlink() or expected.resolve() != expected:
            self._forbidden("The session workspace is outside its isolated path.")
        if not expected.is_dir():
            raise AgentRuntimeError(
                code="workspace_unavailable",
                message="The session workspace no longer exists.",
                status_code=409,
            )
        if expected.stat().st_mode & 0o077:
            self._forbidden("The session workspace permissions must be 0700.")
        return expected

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        suffix = session_id[4:] if session_id.startswith("ags_") else ""
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        if not suffix or any(character not in alphabet for character in suffix):
            raise AgentRuntimeError(
                code="invalid_agent_request",
                message="Invalid gateway session id.",
                status_code=400,
            )

    @staticmethod
    def _forbidden(message: str) -> None:
        raise AgentRuntimeError(
            code="workspace_forbidden",
            message=message,
            status_code=403,
        )
