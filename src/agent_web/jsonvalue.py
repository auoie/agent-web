"""Typed accessors for untrusted JSON (json.loads returns Any)."""

from __future__ import annotations

from typing import cast


def as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def as_list(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def get_str(data: dict[str, object], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def get_int(data: dict[str, object], key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def get_float(data: dict[str, object], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def get_bool(data: dict[str, object], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default
