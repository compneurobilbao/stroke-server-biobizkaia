"""compneuro-step-sdk: the step side of the step contract 1.0."""

from . import _env  # noqa: F401  (must run before numerical libraries are imported)

__version__ = "1.0.0"

from .cli import main
from .context import StepContext
from .errors import InputRejected, InternalFailure, QcFailed
from .runner import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    EXIT_TERMINATED,
    run_step,
)

__all__ = [
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_REJECTED",
    "EXIT_TERMINATED",
    "InputRejected",
    "InternalFailure",
    "QcFailed",
    "StepContext",
    "__version__",
    "main",
    "run_step",
]
