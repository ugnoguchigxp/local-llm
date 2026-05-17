from __future__ import annotations

from core.daemon import _normalize_messages


def test_normalize_messages_truncates_large_tool_call_arguments():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": '{"path":"hoge.md","mode":"w","content":"'
                        + ("X" * 600)
                        + '","edits":[{"old_text":"A","new_text":"B"}]}',
                    },
                }
            ],
        }
    ]

    prepared = _normalize_messages(messages, tools=[])
    assert len(prepared) == 1
    content = prepared[0]["content"]
    assert "Assistant requested tool calls:" in content
    assert "edit_file(" in content
    assert "content=<omitted>" in content
    assert "edits=[1 items]" in content
    assert ("X" * 100) not in content
