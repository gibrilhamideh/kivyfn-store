# src/kivyfn/store/devtools/protocol.py
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

DIAGNOSTIC_VERSION = 2
SAFE_INTEGER = (1 << 53) - 1
_RESERVED = {"$integer", "$object", "$unsupported"}


class CaptureTooLarge(ValueError):
    """A diagnostic representation cannot fit its capture budget."""


def to_wire(value: Any, max_bytes: int = 1024 * 1024) -> Any:
    """Detach inert diagnostic values, preserving exact integer values.

    Stop visiting huge containers as soon as their bounded representation
    cannot fit. Python objects/callables are never evaluated or repr-rendered.
    Literal reserved dictionaries are escaped to prevent tag collisions.
    """
    remaining = max_bytes

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        remaining -= 8
        if remaining < 0 or depth > 64:
            raise CaptureTooLarge("Diagnostic payload exceeds its budget")
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            text = str(item)
            remaining -= len(text)
            return item if abs(item) <= SAFE_INTEGER else {"$integer": text}
        if type(item) is float:
            if math.isfinite(item):
                return item
            return {"$unsupported": "non-finite float"}
        if isinstance(item, str):
            remaining -= len(item.encode("utf-8"))
            if remaining < 0:
                raise CaptureTooLarge("Diagnostic string exceeds its budget")
            return item
        if isinstance(item, Enum):
            return visit(item.value, depth + 1)
        if is_dataclass(item) and not isinstance(item, type):
            return {
                field.name: visit(getattr(item, field.name), depth + 1)
                for field in fields(item)
            }
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, data in item.items():
                if not isinstance(key, str):
                    return {"$unsupported": "mapping with non-string keys"}
                result[visit(key, depth + 1)] = visit(data, depth + 1)
            if len(result) == 1 and set(result) <= _RESERVED:
                return {"$object": list(map(list, result.items()))}
            return result
        if isinstance(item, (tuple, list)):
            return [visit(data, depth + 1) for data in item]
        return {"$unsupported": type(item).__name__}

    result = visit(value)
    if encoded_size(result) > max_bytes:
        raise CaptureTooLarge("Encoded diagnostic payload exceeds its budget")
    return result


def encode_checkpoint(
    checkpoint: dict[str, Any], catalog: dict[str, Any], max_bytes: int
) -> dict[str, Any]:
    """Encode values while preserving structural schema and row maps."""
    result = to_wire(
        {key: value for key, value in checkpoint.items() if key != "schemas"},
        max_bytes,
    )
    result["schemas"] = {}
    remaining = max_bytes - encoded_size(result)
    if remaining < 0:
        raise CaptureTooLarge("Checkpoint exceeds its diagnostic budget")
    for name, data in checkpoint["schemas"].items():
        remaining -= encoded_size(name) + 2
        if catalog[name]["kind"] == "many":
            rows: dict[str, Any] = {}
            result["schemas"][name] = rows
            remaining -= 2
            for row_id, row in data.items():
                remaining -= encoded_size(row_id) + 2
                encoded_row = to_wire(row, remaining)
                remaining -= encoded_size(encoded_row)
                rows[row_id] = encoded_row
        else:
            encoded_data = to_wire(data, remaining)
            remaining -= encoded_size(encoded_data)
            result["schemas"][name] = encoded_data
        if remaining < 0:
            raise CaptureTooLarge("Checkpoint exceeds its diagnostic budget")
    return result


def from_wire(value: Any, depth: int = 0) -> Any:
    """Decode browser value tags at the diagnostic HTTP trust boundary."""
    if depth > 64:
        raise ValueError("Command value nesting exceeds 64 levels")
    if isinstance(value, list):
        return [from_wire(item, depth + 1) for item in value]
    if not isinstance(value, dict):
        if type(value) is int and abs(value) > SAFE_INTEGER:
            raise ValueError("Large integers require a $integer value tag")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Non-finite command values are unsupported")
        return value
    if set(value) == {"$integer"}:
        digits = value["$integer"]
        if (
            not isinstance(digits, str)
            or not digits
            or len(digits) > 4300
            or not digits.removeprefix("-").isascii()
            or not digits.removeprefix("-").isdigit()
        ):
            raise ValueError("Invalid $integer value tag")
        return int(digits)
    if set(value) == {"$unsupported"}:
        raise ValueError("Unsupported diagnostic values cannot be submitted")
    if set(value) == {"$object"}:
        pairs = value["$object"]
        if not isinstance(pairs, list):
            raise ValueError("Invalid $object value tag")
        result: dict[str, Any] = {}
        for pair in pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or pair[0] in result
            ):
                raise ValueError("Invalid $object key/value pair")
            result[pair[0]] = from_wire(pair[1], depth + 1)
        return result
    return {key: from_wire(item, depth + 1) for key, item in value.items()}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encoded_size(value: Any) -> int:
    return len(dumps(value).encode("utf-8"))
