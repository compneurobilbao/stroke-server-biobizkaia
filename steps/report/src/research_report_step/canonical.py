"""Canonical JSON for report.json (the basis for later sealing).

Rules, so that equal content always gives equal bytes on every platform:

* UTF-8, no insignificant whitespace, no trailing newline;
* object keys sorted by Unicode code point;
* non-ASCII characters escaped (``ensure_ascii``);
* integers as plain decimal integers; booleans and null as JSON literals;
* floats rounded half-even to 6 decimals and written in fixed notation, never in
  exponent form, with at least one decimal digit (``1.0``, ``0.25``, ``0.0`` for -0);
  NaN and infinities are rejected.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_EVEN, Decimal

_QUANTUM = Decimal("0.000001")


def format_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("report.json cannot hold NaN or infinity")
    q = Decimal(repr(float(value))).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    if q == 0:
        return "0.0"
    whole, _, frac = format(q, "f").partition(".")
    return f"{whole}.{frac.rstrip('0') or '0'}"


def _encode(value, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(int(value)))
    elif isinstance(value, float):
        out.append(format_float(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=True))
    elif isinstance(value, dict):
        out.append("{")
        for n, key in enumerate(sorted(value)):
            if not isinstance(key, str):
                raise TypeError("object keys must be strings")
            if n:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=True))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for n, item in enumerate(value):
            if n:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise TypeError(f"cannot encode {type(value).__name__}")


def dumps(value) -> str:
    out: list[str] = []
    _encode(value, out)
    return "".join(out)


def dump_bytes(value) -> bytes:
    return dumps(value).encode("utf-8")
