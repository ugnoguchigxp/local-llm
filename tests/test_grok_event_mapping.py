from __future__ import annotations

import pytest

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.grok.event_mapping import map_session_update, permission_summary


def test_maps_message_tool_plan_and_invariant_updates():
    message = map_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    tool = map_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "Edit file",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"secret": "must-not-leak"},
        }
    )
    plan = map_session_update(
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "test", "priority": "medium", "status": "pending"}
            ],
        }
    )
    invariant = map_session_update(
        {"sessionUpdate": "current_model_update", "modelId": "other"}
    )
    config_change = map_session_update(
        {"sessionUpdate": "config_option_update", "configOptions": []}
    )

    assert message is not None and (message.event_type, message.data) == (
        "message.delta",
        {"text": "hi"},
    )
    assert tool is not None and tool.event_type == "tool.started"
    assert "rawInput" not in tool.data and "secret" not in str(tool.data)
    assert plan is not None and plan.event_type == "plan.updated"
    assert invariant is not None and invariant.event_type == "session.invariant_changed"
    assert config_change is not None and config_change.data == {"reason": "config_changed"}


def test_permission_summary_exposes_only_safe_fields():
    session_id, options, subject, allow_once_safe = permission_summary(
        {
            "sessionId": "s1",
            "options": [
                {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"},
            ],
            "toolCall": {
                "toolCallId": "t1",
                "title": "Write a file with xai-secretvalue password=hunter2",
                "kind": "edit",
                "rawInput": {"path": "safe.txt", "token": "secret"},
            },
        }
    )

    assert session_id == "s1"
    assert [item["kind"] for item in options] == ["allow_once", "reject_once"]
    assert subject == {
        "tool_call_id": "t1",
        "title": "Write a file with [redacted] password=[redacted]",
        "kind": "edit",
        "input": {"path": "safe.txt", "token": "[redacted]"},
        "input_available": True,
        "approval_context_complete": False,
    }
    assert allow_once_safe is False


def test_permission_without_tool_input_is_deny_only():
    _session_id, _options, subject, allow_once_safe = permission_summary(
        {
            "sessionId": "s1",
            "options": [
                {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"},
            ],
            "toolCall": {"toolCallId": "t1", "title": "Run operation"},
        }
    )

    assert subject["input_available"] is False
    assert subject["approval_context_complete"] is False
    assert "input" not in subject
    assert allow_once_safe is False


def test_complete_bounded_tool_input_can_be_approved_once():
    _session_id, _options, subject, allow_once_safe = permission_summary(
        {
            "sessionId": "s1",
            "options": [
                {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"},
            ],
            "toolCall": {"toolCallId": "t1", "rawInput": {"path": "safe.txt"}},
        }
    )

    assert subject["approval_context_complete"] is True
    assert allow_once_safe is True


def test_malformed_known_updates_fail_closed():
    with pytest.raises(AgentRuntimeError, match="content must be text"):
        map_session_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "image"}})

    with pytest.raises(AgentRuntimeError, match="unique"):
        permission_summary(
            {
                "sessionId": "s1",
                "options": [
                    {"optionId": "same", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "same", "name": "Deny", "kind": "reject_once"},
                ],
                "toolCall": {"toolCallId": "t1"},
            }
        )

    with pytest.raises(AgentRuntimeError, match="plan entry"):
        map_session_update(
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "test", "priority": "urgent", "status": "pending"}
                ],
            }
        )

    with pytest.raises(AgentRuntimeError, match="tool kind"):
        map_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Run",
                "kind": "future_tool_kind",
            }
        )
