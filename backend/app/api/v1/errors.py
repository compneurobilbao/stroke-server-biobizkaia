"""Errors of the v1 API: a body with ``detail`` and ``code`` (plus ``slot`` when a slot is meant).

Codes of the legacy catalogue (``app.core.errors``) are reused where they exist
(``not_authenticated``, ``permission_denied``, ``storage_quota_exceeded``,
``upload_too_large``); the others are listed in docs/api.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import ApiError


class V1Error(ApiError):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        slot: str | None = None,
        reasons: Sequence[Mapping[str, str]] = (),
    ) -> None:
        # ApiError.__init__ looks the code up in the legacy catalogue; bypass it.
        super(ApiError, self).__init__(status_code=status_code, detail=detail)
        self.code = code
        self.slot = slot
        # Every reason of a refused plan (run-plans.md 3.3); ``code`` is the first one's.
        self.reasons = [dict(r) for r in reasons]


def v1_error(
    status_code: int, code: str, detail: str, slot: str | None = None
) -> V1Error:
    return V1Error(status_code, code, detail, slot)


async def v1_error_handler(_: Request, exc: V1Error) -> JSONResponse:
    body: dict = {"detail": exc.detail, "code": exc.code}
    if exc.slot:
        body["slot"] = exc.slot
    if exc.reasons:
        body["reasons"] = exc.reasons
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


# HTTP status of the refusal codes of run creation and plan preview (run-plans.md 3.3).
# A code that is in none of the sets is a conflict with the deployment or the case
# state (409): ``no_implementation``, ``slot_unsatisfiable``, ``step_contract_too_old``,
# ``manifest_unavailable``, ``bundle_unavailable``.
REFUSAL_FORBIDDEN = frozenset({"free_mode_not_allowed"})
REFUSAL_NOT_FOUND = frozenset({"preset_not_found"})
REFUSAL_UNPROCESSABLE = frozenset(
    {
        "unknown_slot",
        "implementation_not_enabled",
        "input_type_mismatch",
        "no_matching_input_set",
        "input_not_in_case",
        "invalid_parameters",
        "implementation_invalid",
        # plans
        "preset_not_enabled",
        "preset_input_set_mismatch",
        "preset_and_targets",
        "unknown_target",
        "ambiguous_target",
        "target_not_applicable",
        "target_not_guaranteed",
        "target_unreachable",
        "conflicting_targets",
        "empty_request",
        "implementation_fixed_by_preset",
    }
)


def refusal_status(code: str) -> int:
    if code in REFUSAL_FORBIDDEN:
        return 403
    if code in REFUSAL_NOT_FOUND:
        return 404
    return 422 if code in REFUSAL_UNPROCESSABLE else 409


def permission_denied() -> V1Error:
    return v1_error(
        403, "permission_denied", "You do not have permission to perform this operation"
    )


def case_not_found() -> V1Error:
    return v1_error(404, "case_not_found", "Case not found")


def run_not_found() -> V1Error:
    return v1_error(404, "run_not_found", "Run not found")
