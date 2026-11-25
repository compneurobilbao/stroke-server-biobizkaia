"""The author's code: ``src/cdr.py`` of the COGNET-STROKE repository, unmodified.

The file is loaded from ``$COGNET_STROKE_SRC/src/cdr.py`` (``/opt/upstream/COGNET-STROKE`` in
the image, where the Dockerfile copies it from the commit pinned in ``upstream.env``) under a
module name of its own, so that the repository's top-level package name ``src`` never enters
``sys.modules``. Nothing of the file is changed or re-implemented here: the wrapper calls its
``compute_cdr`` and ``compute_overlap``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

DEFAULT_ROOT = Path("/opt/upstream/COGNET-STROKE")
MODULE_NAME = "cognet_stroke_upstream_cdr"
NAME = "COGNET-STROKE"


class UpstreamUnavailable(RuntimeError):
    """The author's code is not where the step expects it."""


def root() -> Path:
    return Path(os.environ.get("COGNET_STROKE_SRC") or DEFAULT_ROOT)


def cdr_path() -> Path:
    return root() / "src" / "cdr.py"


@lru_cache(maxsize=None)
def _load(path: str) -> ModuleType:
    file = Path(path)
    if not file.is_file():
        raise UpstreamUnavailable(f"{NAME} src/cdr.py not found under {file.parent.parent}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    for name in ("compute_cdr", "compute_overlap"):
        if not callable(getattr(module, name, None)):
            raise UpstreamUnavailable(f"{NAME} src/cdr.py has no {name}")
    return module


def load() -> ModuleType:
    """The author's ``cdr`` module (``compute_cdr``, ``compute_overlap``)."""
    return _load(str(cdr_path()))


def commit() -> str:
    """Commit of the author's repository the code was taken from (``UPSTREAM_COMMIT``)."""
    marker = root() / "UPSTREAM_COMMIT"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "unknown"
