"""Minimal JSON-Schema checks for order-argument contract tests.

Only the keywords used by the recorded MCP tool schemas are supported:
``type``, ``properties``, ``required``, ``additionalProperties``, ``items``.
"""

from __future__ import annotations

from typing import Any


def validate(schema: dict[str, Any], instance: Any) -> list[str]:
    """Return a list of human-readable contract violations (empty == valid)."""
    errors: list[str] = []
    _validate(schema, instance, path="$", errors=errors)
    return errors


def _validate(
    schema: dict[str, Any], instance: Any, *, path: str, errors: list[str]
) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(instance, dict):
            errors.append(f"{path}: expected object, got {type(instance).__name__}")
            return
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: unexpected property {key!r}")
        for key, value in instance.items():
            if key in properties:
                _validate(
                    properties[key], value, path=f"{path}.{key}", errors=errors
                )
        return

    if expected == "array":
        if not isinstance(instance, list):
            errors.append(f"{path}: expected array, got {type(instance).__name__}")
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate(item_schema, item, path=f"{path}[{index}]", errors=errors)
        return

    if expected == "string":
        if not isinstance(instance, str):
            errors.append(f"{path}: expected string, got {type(instance).__name__}")
        return

    if expected == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            errors.append(f"{path}: expected integer, got {type(instance).__name__}")
        return


def tool_schema(tools: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for tool in tools:
        if tool.get("name") == name:
            schema = tool.get("inputSchema")
            if isinstance(schema, dict):
                return schema
    raise AssertionError(f"tool {name!r} not present in snapshot")
