from __future__ import annotations

import json

from core.context_budget import compress_messages


def test_compress_messages_drops_debug_raw_payload_from_large_json():
    large_payload = {
        "section": "large_json_payloads",
        "requiredOutputFormat": {"type": "json_object"},
        "debugRawPayload": "X" * 10000,
        "items": [{"id": index, "text": "value"} for index in range(80)],
    }
    messages = [
        {"role": "system", "content": "system instructions\nrequired output format must survive"},
        {"role": "user", "content": json.dumps(large_payload)},
        {"role": "user", "content": "current user request must survive"},
    ]

    result = compress_messages(
        messages,
        safe_prompt_budget_tokens=1000,
        estimated_prompt_tokens=10000,
    )

    assert result.compression_applied is True
    assert result.compressed_sections == ["large_json_payloads"]
    assert result.dropped_fields == ["debugRawPayload"]
    assert result.messages[0]["content"] == messages[0]["content"]
    assert result.messages[-1]["content"] == "current user request must survive"
    assert "debugRawPayload" not in result.messages[1]["content"]
    assert "requiredOutputFormat" in result.messages[1]["content"]
