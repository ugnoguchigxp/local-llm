from __future__ import annotations

import pytest

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.workspaces import WorkspaceManager


def test_workspace_manager_creates_private_isolated_directory(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")

    workspace = manager.create_isolated("ags_abc123")

    assert workspace.is_dir()
    assert workspace.parent == (tmp_path / "workspaces").resolve()
    assert workspace.stat().st_mode & 0o777 == 0o700


def test_workspace_manager_rejects_untrusted_session_id(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")

    with pytest.raises(AgentRuntimeError, match="Invalid gateway session id"):
        manager.create_isolated("ags_../escape")


def test_workspace_manager_rejects_existing_symlink_inside_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    target = root / "other"
    target.mkdir()
    (root / "ags_abc123").symlink_to(target, target_is_directory=True)
    manager = WorkspaceManager(root)

    with pytest.raises(AgentRuntimeError) as raised:
        manager.create_isolated("ags_abc123")

    assert raised.value.code == "workspace_forbidden"


def test_workspace_manager_validates_exact_session_path(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create_isolated("ags_abc123")

    assert manager.validate_isolated("ags_abc123", workspace) == workspace

    other = tmp_path / "outside"
    other.mkdir()
    with pytest.raises(AgentRuntimeError) as raised:
        manager.validate_isolated("ags_abc123", other)
    assert raised.value.code == "workspace_forbidden"
