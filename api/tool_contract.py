from __future__ import annotations

from typing import Any

from core.tool_calling import normalize_tool_name


def _tool_function(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    return function if isinstance(function, dict) else {}


def tool_name(tool: dict[str, Any]) -> str:
    return normalize_tool_name(str(_tool_function(tool).get("name", "")))


def resolve_tool_choice(
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], set[str], str, str | None]:
    available = {tool_name(tool) for tool in tools if tool_name(tool)}

    if not tools:
        if isinstance(tool_choice, dict):
            return [], set(), "forced", "tool_choice requires tools, but no tools were provided"
        if isinstance(tool_choice, str) and tool_choice.lower() == "required":
            return [], set(), "required", "tool_choice=required requires tools, but no tools were provided"
        return [], set(), "none", None

    if tool_choice is None:
        return tools, available, "auto", None

    if isinstance(tool_choice, str):
        choice = tool_choice.lower()
        if choice == "none":
            return [], set(), "none", None
        if choice == "auto":
            return tools, available, "auto", None
        if choice == "required":
            return tools, available, "required", None
        return [], set(), choice, f"unsupported tool_choice '{tool_choice}'"

    function = tool_choice.get("function") if isinstance(tool_choice, dict) else None
    forced_name = function.get("name") if isinstance(function, dict) else None
    if isinstance(forced_name, str) and forced_name:
        normalized = normalize_tool_name(forced_name)
        if normalized not in available:
            return [], set(), "forced", f"tool_choice function '{forced_name}' is not present in tools"
        filtered = [tool for tool in tools if tool_name(tool) == normalized]
        return filtered, {normalized}, "forced", None

    return [], set(), "forced", "tool_choice function name is required"


def _type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> str | None:
    raw_type = schema.get("type")
    expected_types: list[str] = []
    if isinstance(raw_type, str):
        expected_types = [raw_type]
    elif isinstance(raw_type, list):
        expected_types = [item for item in raw_type if isinstance(item, str)]

    if expected_types and not any(_type_matches(value, expected) for expected in expected_types):
        return f"{path} must be {', '.join(expected_types)}"

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        return f"{path} must be one of {enum_values}"

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    return f"{path}.{key} is required"

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key not in value or not isinstance(child_schema, dict):
                    continue
                error = _validate_schema_value(value[key], child_schema, f"{path}.{key}")
                if error:
                    return error

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                error = _validate_schema_value(item, items, f"{path}[{index}]")
                if error:
                    return error

    return None


def validate_tool_arguments(
    parsed_tool_call: dict[str, Any],
    tools: list[dict[str, Any]],
) -> str | None:
    name = normalize_tool_name(str(parsed_tool_call.get("name", "")))
    arguments = parsed_tool_call.get("arguments", {})
    if not isinstance(arguments, dict):
        return f"tool '{name}' arguments must be an object"

    matching_tool = next((tool for tool in tools if tool_name(tool) == name), None)
    if matching_tool is None:
        return f"tool '{name}' is not available"

    parameters = _tool_function(matching_tool).get("parameters")
    if not isinstance(parameters, dict):
        return None
    return _validate_schema_value(arguments, parameters, "arguments")


def build_tool_retry_message(tool_name_value: str, validation_error: str) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            f"The previous function call for '{tool_name_value}' had invalid arguments: "
            f"{validation_error}. Return only one JSON object in this exact shape: "
            '{"name":"<function_name>","arguments":{"arg":"value"}}. '
            "Use the provided function schema. Do not include markdown or explanation."
        ),
    }
