from __future__ import annotations

import json

from core.tool_calling import parse_tool_call


def test_parse_tool_call_accepts_tool_name():
    parsed = parse_tool_call('{"tool_name":"list_directory","arguments":{"path":"/tmp"}}')
    assert parsed == {"name": "list_directory", "arguments": {"path": "/tmp"}}


def test_parse_tool_call_ignores_thinking_block_before_payload():
    parsed = parse_tool_call(
        '<think>{"name":"wrong","arguments":{"path":"/bad"}}</think>'
        '{"tool_name":"list_directory","arguments":{"path":"/tmp"}}'
    )
    assert parsed == {"name": "list_directory", "arguments": {"path": "/tmp"}}


def test_parse_tool_call_accepts_function_name_and_args():
    parsed = parse_tool_call('{"function_name":"list_directory","args":{"path":"/tmp"}}')
    assert parsed == {"name": "list_directory", "arguments": {"path": "/tmp"}}


def test_parse_tool_call_respects_allowed_tool_names():
    parsed = parse_tool_call(
        '{"tool_name":"list_directory","arguments":{"path":"/tmp"}}',
        allowed_tool_names={"search_web"},
    )
    assert parsed is None


def test_parse_tool_call_preserves_nested_argument_types():
    payload = {
        "tool_name": "edit_file",
        "arguments": {
            "path": "/tmp/a.txt",
            "edits": [{"old_text": "A", "new_text": "B"}],
            "dry_run": False,
            "limit": 3,
            "content": "line1\nline2",
        },
    }
    parsed = parse_tool_call(json.dumps(payload))
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"]["path"] == "/tmp/a.txt"
    assert parsed["arguments"]["edits"] == [{"old_text": "A", "new_text": "B"}]
    assert parsed["arguments"]["dry_run"] is False
    assert parsed["arguments"]["limit"] == 3
    assert parsed["arguments"]["content"] == "line1\nline2"


def test_parse_tool_call_accepts_assistant_requested_tool_calls_format():
    text = (
        "Assistant requested tool calls:\n"
        '- edit_file({"path":"hoge.md","mode":"w","content":"Hello"})'
    )
    parsed = parse_tool_call(text)
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"] == {"path": "hoge.md", "mode": "w", "content": "Hello"}


def test_parse_tool_call_accepts_parenthesized_payload_with_raw_newline_in_string():
    text = (
        "Assistant requested tool calls:\n"
        '- edit_file({"path":"hoge.md","mode":"w","content":"line1\nline2"})'
    )
    parsed = parse_tool_call(text)
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"]["content"] == "line1\nline2"


def test_parse_tool_call_accepts_parenthesized_payload_even_if_json_tail_is_missing():
    text = (
        "Assistant requested tool calls:\n"
        '- edit_file({"path":"hoge.md","mode":"w","content":"hello"'
    )
    parsed = parse_tool_call(text)
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"]["path"] == "hoge.md"
    assert parsed["arguments"]["mode"] == "w"
    assert parsed["arguments"]["content"] == "hello"


def test_parse_tool_call_accepts_python_kwargs_style_parenthesized_payload():
    text = (
        "Assistant requested tool calls:\n"
        "- edit_file({ display_description='Creating hoge.md', path='hoge.md', mode='w', content='Hello' })"
    )
    parsed = parse_tool_call(text, allowed_tool_names={"edit_file"})
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"]["display_description"] == "Creating hoge.md"
    assert parsed["arguments"]["path"] == "hoge.md"
    assert parsed["arguments"]["mode"] == "w"
    assert parsed["arguments"]["content"] == "Hello"


def test_parse_tool_call_accepts_truncated_python_kwargs_style_parenthesized_payload():
    text = (
        "Assistant requested tool calls:\n"
        "- edit_file({ display_description='Final attempt', path='hoge.md', mode='w',"
    )
    parsed = parse_tool_call(text, allowed_tool_names={"edit_file"})
    assert parsed is not None
    assert parsed["name"] == "edit_file"
    assert parsed["arguments"]["display_description"] == "Final attempt"
    assert parsed["arguments"]["path"] == "hoge.md"
    assert parsed["arguments"]["mode"] == "w"
