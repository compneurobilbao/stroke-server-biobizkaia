"""Process environment set-up that must happen before numerical libraries load.

Imported first by the package ``__init__``. Contract section 2.3: a step must not
use more threads than ``STEP_CPUS``; section 6: nothing may be written outside
``/out`` and ``/tmp``, caches included.
"""

from __future__ import annotations

import os

THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
)


def _granted_cpus(environ) -> int:
    try:
        value = int(environ.get("STEP_CPUS", "1"))
    except ValueError:
        return 1
    return max(value, 1)


def apply(environ=None) -> None:
    """Cap thread pools at ``STEP_CPUS`` and redirect caches to the temp directory."""
    env = os.environ if environ is None else environ
    if "STEP_CPUS" in env:
        cpus = str(_granted_cpus(env))
        for name in THREAD_VARIABLES:
            env.setdefault(name, cpus)
    tmp = env.get("TMPDIR", "/tmp")
    env.setdefault("XDG_CACHE_HOME", os.path.join(tmp, ".cache"))
    env.setdefault("MPLCONFIGDIR", os.path.join(tmp, ".mplconfig"))
    env.setdefault("NUMBA_CACHE_DIR", os.path.join(tmp, ".numba"))
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")


def cpus(environ=None) -> int:
    return _granted_cpus(os.environ if environ is None else environ)


apply()
