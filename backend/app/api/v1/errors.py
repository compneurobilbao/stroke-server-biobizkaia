"""Errors of the v1 API: a body with ``detail`` and ``code`` (plus ``slot`` when a slot is meant).

Codes of the legacy catalogue (``app.core.errors``) are reused where they exist
(``not_authenticated``, ``permission_denied``, ``storage_quota_exceeded``,
``upload_too_large``); the others are listed in docs/api.md.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import ApiError


class V1Error(ApiError):
    def __init__(
        self, status_code: int, code: str, detail: str, slot: str | None = None
    ) -> None:
        # ApiError.__init__ looks the code up in the legacy catalogue; bypass it.
        super(ApiError, self).__init__(status_code=status_code, detail=detail)
        self.code = code
        self.slot = slot


def v1_error(
    status_code: int, code: str, detail: str, slot: str | None = None
) -> V1Error:
    return V1Error(status_code, code, detail, slot)


async def v1_error_handler(_: Request, exc: V1Error) -> JSONResponse:
    body = {"detail": exc.detail, "code": exc.code}
    if exc.slot:
        body["slot"] = exc.slot
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


def permission_denied() -> V1Error:
    return v1_error(
        403, "permission_denied", "You do not have permission to perform this operation"
    )


def case_not_found() -> V1Error:
    return v1_error(404, "case_not_found", "Case not found")


def run_not_found() -> V1Error:
    return v1_error(404, "run_not_found", "Run not found")
