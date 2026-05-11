import json
import re
from typing import Any, Dict, Optional

REPAIR_PROMPT_TEMPLATE = """以下の指示（Instruction）および前回の失敗フィードバック（Previous Feedback）に基づき、修正したパッチを生成してください。

### 指示 (Instruction)
{instruction}

### フィードバック (Feedback)
- 試行回数: {attempt}
{rejections_section}
{issues_section}

### 出力要件 (IMPORTANT)
1. 返却は必ず **Astmend operation JSON 1件のみ** としてください。
2. JSON以外の文字列（説明文、Markdownのコードブロック記号 ```json 等）は一切含めないでください。
3. 指定された `targetFiles` 以外のファイルへの影響は最小限に抑えてください。
"""

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def is_repair_input(data: Any) -> bool:
    """Check if the input data corresponds to a repair mode request."""
    if not isinstance(data, dict):
        return False
    task = data.get("task")
    feedback = data.get("feedback")
    return isinstance(task, dict) and isinstance(feedback, dict)


def _extract_json_candidate(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    fence_match = JSON_FENCE_RE.fullmatch(stripped)
    if fence_match:
        return fence_match.group(1).strip()

    return None


def detect_repair_json(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to parse text as a repair mode JSON."""
    candidate = _extract_json_candidate(text)
    if not candidate:
        return None

    try:
        data = json.loads(candidate)
        if is_repair_input(data):
            return data
    except json.JSONDecodeError:
        pass

    return None


def format_repair_prompt(data: Dict[str, Any]) -> str:
    """Format the repair JSON into a natural language prompt for the LLM."""
    task = data.get("task", {}) if isinstance(data.get("task"), dict) else {}
    feedback = data.get("feedback", {}) if isinstance(data.get("feedback"), dict) else {}
    
    instruction = task.get("instruction", "No instruction provided.")
    attempt = feedback.get("attempt", 1)
    
    # Format Rejections
    rejections = feedback.get("previousRejects", [])
    rejections_section = ""
    if rejections:
        rejections_section = "\n#### 却下された変更 (Previous Rejections):\n"
        for r in rejections:
            if not isinstance(r, dict):
                continue
            path = r.get("path", "unknown")
            reason = r.get("reason", "No reason provided")
            rejections_section += f"- ファイル: `{path}`, 理由: `{reason}`\n"
            
    # Format Issues
    issues = feedback.get("previousIssues", [])
    issues_section = ""
    if issues:
        issues_section = "\n#### 検出された問題 (Previous Issues):\n"
        for i in issues:
            if not isinstance(i, dict):
                continue
            level = i.get("level", "error").upper()
            msg = i.get("message", "No message")
            issue_id = i.get("id", "N/A")
            issues_section += f"- [{level}] {issue_id}: {msg}\n"

    return REPAIR_PROMPT_TEMPLATE.format(
        instruction=instruction,
        attempt=attempt,
        rejections_section=rejections_section,
        issues_section=issues_section
    )
