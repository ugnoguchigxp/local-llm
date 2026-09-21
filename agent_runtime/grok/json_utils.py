from __future__ import annotations

import json
from typing import Any


def loads_strict(document: str | bytes | bytearray) -> Any:
    return json.loads(
        document,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
