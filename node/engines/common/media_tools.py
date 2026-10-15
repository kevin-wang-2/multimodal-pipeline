"""Small shared schema and error helpers for deterministic media engines."""
from __future__ import annotations

import os
import math


class MediaError(ValueError):
    """An unsupported/invalid input or operation, not an internal engine fault."""


def integer(low=0, high=32768, **extra):
    return {"type": "integer", "minimum": low, "maximum": high, **extra}


def number(low, high, **extra):
    return {"type": "number", "minimum": low, "maximum": high, **extra}


def choice(*values, **extra):
    return {"type": "string", "enum": list(values), **extra}


def options(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def limit(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def finite_options(value):
    # Python's JSON parser accepts NaN, even though it is not a JSON number.
    if isinstance(value, float) and not math.isfinite(value):
        raise MediaError("invalid_argument: numeric parameters must be finite")
    if isinstance(value, dict):
        for item in value.values():
            finite_options(item)
    elif isinstance(value, list):
        for item in value:
            finite_options(item)
