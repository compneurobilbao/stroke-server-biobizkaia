"""Format rules of the data model (docs/data-model.md section 2, item 4).

The same patterns are enforced as PostgreSQL `CHECK` constraints by migration 0011 (which
carries its own copy, so that the migration does not change when this module does) and by
the validators of the models and of the API schemas.
"""

from __future__ import annotations

import re

SHA256_HEX = r"^[0-9a-f]{64}$"
DIGEST = r"^sha256:[0-9a-f]{64}$"
IMAGE_REF = (
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+@sha256:[a-f0-9]{64}$"
)
KEBAB = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
SNAKE = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
SEMVER = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
SUBJECT_LABEL = r"^[0-9a-f]{12}$"
AGE_BAND_FORMAT = r"^[0-9]{1,3}-[0-9]{1,3}$"

_COMPILED = {
    "sha256hex": re.compile(SHA256_HEX),
    "digest": re.compile(DIGEST),
    "image_ref": re.compile(IMAGE_REF),
    "kebab": re.compile(KEBAB),
    "snake": re.compile(SNAKE),
    "semver": re.compile(SEMVER),
    "subject_label": re.compile(SUBJECT_LABEL),
}


def matches(kind: str, value: str) -> bool:
    return bool(_COMPILED[kind].match(value))


def check_format(kind: str, value: str | None) -> str | None:
    """Return the value unchanged if it matches `kind` (or is None); raise ValueError."""
    if value is not None and not matches(kind, value):
        raise ValueError(f"value does not match the {kind} format")
    return value
