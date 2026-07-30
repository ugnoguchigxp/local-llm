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
    assert len(prepared) == 2
    content = prepared[1]["content"]
    assert "Assistant requested tool calls:" in content
    assert "call_1:" in content
    assert "edit_file(" in content
    assert "content=<omitted>" in content
    assert "edits=[1 items]" in content
    assert ("X" * 100) not in content


def test_normalize_messages_preserves_tool_result_call_id():
    prepared = _normalize_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "file text"},
        ],
        tools=[],
    )

    contents = "\n".join(message["content"] for message in prepared)
    assert "call_abc: read_file" in contents
    assert "Tool result (call_abc):" in contents
    assert "file text" in contents
