from __future__ import annotations

from speech.tts.instruct import build_instruct
from speech.tts.schemas import SpeechRequest


def test_instruct_combines_free_text_preset_style_and_speed() -> None:
    request = SpeechRequest(
        model="qwen3-tts-0.6b-custom-voice",
        input="テストです。",
        voice="ono_anna",
        instructions="優しく話してください。",
        speed=0.8,
        qwen={
            "style_preset": "calm_narration",
            "style": {"whisper": False, "accent": "標準語"},
        },
    )

    instruction, digest = build_instruct(request)

    assert instruction is not None
    assert "優しく" in instruction
    assert "穏やか" in instruction
    assert "標準語" in instruction
    assert "ゆっくり" in instruction
    assert "ささやき声にはしない" in instruction
    assert len(digest) == 16


def test_instruct_is_stable() -> None:
    request = SpeechRequest(
        model="qwen3-tts-0.6b-custom-voice",
        input="同じ入力",
        voice="ono_anna",
    )
    assert build_instruct(request) == build_instruct(request)
