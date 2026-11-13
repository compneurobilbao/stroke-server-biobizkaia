"""Exceptions of the step SDK."""

from __future__ import annotations

from typing import Any


class StepRejection(Exception):
    """Base of the two exit-10 outcomes. Use ``InputRejected`` or ``QcFailed``."""

    category = ""

    def __init__(
        self,
        code: str,
        message: str,
        port: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.port = port
        self.details = details


class InputRejected(StepRejection):
    """The input is unsuitable (format, constraint); retrying will not help. Exit 10."""

    category = "input_rejected"


class QcFailed(StepRejection):
    """A quality-control check failed; retrying will not help. Exit 10."""

    category = "qc_failed"


class InternalFailure(Exception):
    """A failure of the step or of its environment (exit 1, category ``internal``)."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ManifestError(Exception):
    """The step manifest is missing or invalid."""


class Terminated(BaseException):
    """Raised in the main thread when ``SIGTERM`` (or ``SIGINT``) arrives."""

    def __init__(self, signum: int = 15) -> None:
        super().__init__(signum)
        self.signum = signum
