"""Access control, case and run lookup, audit, and service providers of the v1 API.

Permission matrix: docs/data-model.md section 9. Phase 0 enforces the ``admin`` and
``researcher`` columns. ``reviewer`` and ``coordinator`` act on *granted* cases and
no grants exist before F1-03, so they see no case; ``service`` is refused everywhere.
Rules, in this order:

1. no valid token: 401 (``get_current_user``);
2. the role has no scope for the action: 403;
3. a case or run the user cannot see (not owned, deleted, legacy, or not granted):
   404, so that existence is not revealed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from sqlalchemy import false, select
from sqlalchemy.orm import Session

from app.api.v1.errors import (
    V1Error,
    case_not_found,
    permission_denied,
    run_not_found,
)
from app.core.config import Settings, free_mode_allowed, run_plan_policy
from app.core.validation import (
    DirectoryValidationLevels,
    StaticValidationLevels,
    min_implementation_validation,
)
from app.models.audit_event import AuditEvent
from app.models.case import Case, DeploymentProfile
from app.models.pipeline_run import LEGACY_PIPELINE_ID, PipelineRun
from app.models.user import User
from app.services.storage import LocalStudyStorage
from pathlib import Path
from orchestrator.bundle_resolver import BundleResolver, RegistryBundleResolver
from orchestrator.catalog import (
    DirectoryPipelineRegistry,
    DirectoryPresetRegistry,
    DirectoryStepCatalog,
    PipelineRegistry,
    PresetRegistry,
    StaticPresetRegistry,
    StepCatalog,
    resolve_presets_dir,
)

log = logging.getLogger(__name__)


class Scope(str, Enum):
    all = "all"
    own = "own"
    granted = "granted"


_A, _O, _G = Scope.all, Scope.own, Scope.granted
_ADMIN, _RES, _REV, _COORD = "admin", "researcher", "reviewer", "coordinator"

# action -> {role: scope}; a missing role means "no". Mirrors docs/data-model.md section 9.
ACTIONS: dict[str, dict[str, Scope]] = {
    "case.read": {_ADMIN: _A, _RES: _O, _REV: _G, _COORD: _G},
    "case.create": {_ADMIN: _A, _RES: _O},
    "case.edit": {_ADMIN: _A, _RES: _O, _COORD: _G},
    "case.inputs.write": {_ADMIN: _A, _RES: _O},
    "case.inputs.download": {_ADMIN: _A, _RES: _O, _REV: _G},
    "case.delete": {_ADMIN: _A, _RES: _O},
    "case.keep_forever": {_ADMIN: _A},
    "pipeline.list": {_ADMIN: _A, _RES: _A, _REV: _A, _COORD: _A},
    "run.create": {_ADMIN: _A, _RES: _O},
    "run.read": {_ADMIN: _A, _RES: _O, _REV: _G, _COORD: _G},
    "run.logs": {_ADMIN: _A, _RES: _O},
    "run.cancel": {_ADMIN: _A, _RES: _O},
    "run.resume": {_ADMIN: _A, _RES: _O},
    "artifact.download": {_ADMIN: _A, _RES: _O, _REV: _G, _COORD: _G},
    "report.download": {_ADMIN: _A, _RES: _O, _REV: _G, _COORD: _G},
    "implementation.manage": {_ADMIN: _A},
    "preset.manage": {_ADMIN: _A},
}


def authorize(user: User, action: str) -> Scope:
    scope = ACTIONS[action].get(user.role)
    if scope is None:
        raise permission_denied()
    return scope


def granted_case_ids(db: Session, user: User) -> set[UUID]:
    """Cases the user holds a grant for. Grants arrive with F1-03; none exist before."""
    return set()


def visible_cases(db: Session, user: User, action: str = "case.read"):
    """``select(Case)`` limited to what ``user`` may use for ``action`` (not deleted, not legacy)."""
    scope = authorize(user, action)
    query = select(Case).where(
        Case.deleted_at.is_(None),
        Case.deployment_profile != DeploymentProfile.legacy.value,
    )
    if scope is Scope.own:
        query = query.where(Case.owner_user_id == user.id)
    elif scope is Scope.granted:
        ids = granted_case_ids(db, user)
        query = query.where(Case.id.in_(ids)) if ids else query.where(false())
    return query


def load_case(db: Session, case_id: UUID, user: User, action: str) -> Case:
    case = db.scalar(visible_cases(db, user, action).where(Case.id == case_id))
    if case is None:
        raise case_not_found()
    return case


def load_run(
    db: Session, run_id: UUID, user: User, action: str
) -> tuple[PipelineRun, Case]:
    authorize(user, action)
    run = db.get(PipelineRun, run_id)
    if run is None or run.pipeline_id == LEGACY_PIPELINE_ID:
        raise run_not_found()
    try:
        case = load_case(db, run.case_id, user, action)
    except V1Error as exc:
        if exc.status_code == 404:
            raise run_not_found() from None
        raise
    return run, case


def audit_event(
    db: Session,
    event_type: str,
    user: User,
    *,
    case_id: UUID | None = None,
    run_id: UUID | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """Audit row of a user action. Details never hold file names or free text."""
    db.add(
        AuditEvent(
            case_id=case_id,
            pipeline_run_id=run_id,
            event_type=event_type,
            details=json.dumps(details or {}),
            actor=f"user:{user.id}",
            actor_user_id=user.id,
            ip_address=ip_address,
        )
    )


def storage_for(settings: Settings) -> LocalStudyStorage:
    return LocalStudyStorage(settings.storage_root)


def deployment_profile() -> str:
    value = os.environ.get(
        "DEPLOYMENT_PROFILE", DeploymentProfile.research_public.value
    )
    allowed = {p.value for p in DeploymentProfile} - {DeploymentProfile.legacy.value}
    if value not in allowed:
        raise RuntimeError(f"DEPLOYMENT_PROFILE must be one of {sorted(allowed)}")
    return value


def effective_run_plan_policy() -> str:
    """``RUN_PLAN_POLICY`` of this deployment (run-plans.md 3.5), read from the
    environment on each call like ``deployment_profile``. Raises ``RuntimeError`` for an
    unknown value and for any value other than ``presets_only`` on a public profile."""
    return run_plan_policy(deployment_profile())


def effective_min_validation() -> str:
    """``MIN_IMPLEMENTATION_VALIDATION`` of this deployment (run-plans.md 4.4), read from
    the environment on each call. Raises ``RuntimeError`` for an unknown value and for any
    value below ``validated`` on a public profile."""
    return min_implementation_validation(deployment_profile())


def may_use_free_mode(user: User) -> bool:
    """Whether ``user`` may start runs with targets under this deployment's policy."""
    return free_mode_allowed(effective_run_plan_policy(), user.role)


def require_free_mode(user: User) -> None:
    """403 ``free_mode_not_allowed`` unless the policy lets ``user`` use free mode."""
    if not may_use_free_mode(user):
        raise V1Error(
            403,
            "free_mode_not_allowed",
            "Free mode (a run with targets) is not allowed for you in this "
            "deployment; choose a preset or the full plan.",
        )


# ------------------------------------------------------------ service providers


@dataclass(frozen=True)
class Orchestration:
    """What the API needs from the orchestrator package (loaded once per process)."""

    pipelines: PipelineRegistry
    steps: StepCatalog
    bundles: BundleResolver
    platform_version: str
    presets: PresetRegistry = field(default_factory=StaticPresetRegistry)
    # Validation level of every implementation (registry.yaml), for the admin enable
    # check (card F1-23); without registry data an implementation is unvalidated.
    validation: DirectoryValidationLevels | StaticValidationLevels = field(
        default_factory=StaticValidationLevels
    )


@lru_cache(maxsize=1)
def _default_orchestration() -> Orchestration:
    from app.db.session import SessionLocal
    from orchestrator.resolution import recorded_preset_digests

    env = os.environ
    pipelines_dir = env.get("PIPELINES_DIR", "/app/pipelines")
    pipelines = DirectoryPipelineRegistry(pipelines_dir)
    steps = DirectoryStepCatalog(env.get("STEP_MANIFESTS_DIR", "/app/steps"))
    presets_dir = resolve_presets_dir(pipelines_dir, env)
    # Rule P8: a preset file that differs from the one earlier runs recorded for the
    # same version is not offered (a changed preset needs a new version). The database
    # is read only when there are preset files to check.
    recorded = {}
    if any(Path(presets_dir).rglob("presets/*.yaml")):
        with SessionLocal() as db:
            recorded = recorded_preset_digests(db)
    return Orchestration(
        pipelines=pipelines,
        steps=steps,
        bundles=RegistryBundleResolver(
            env.get("BUNDLE_ROOT", "/app/data/bundles"), SessionLocal
        ),
        platform_version=env.get("PLATFORM_VERSION", "0.0.0-dev"),
        presets=DirectoryPresetRegistry(presets_dir, pipelines, steps, recorded),
        validation=DirectoryValidationLevels(pipelines_dir),
    )


def get_orchestration() -> Orchestration:
    return _default_orchestration()


class RunQueue(Protocol):
    def enqueue(self, run_id: UUID) -> str | None: ...

    def revoke(self, task_id: str) -> None: ...


class CeleryRunQueue:
    def enqueue(self, run_id: UUID) -> str | None:
        from worker.tasks import run_pipeline

        result = run_pipeline.delay(str(run_id))
        return getattr(result, "id", None)

    def revoke(self, task_id: str) -> None:
        from worker.celery_app import celery_app

        celery_app.control.revoke(task_id)


def get_run_queue() -> RunQueue:
    return CeleryRunQueue()


def lock_case(db: Session, case_id: UUID) -> Case:
    """Re-read the case row under ``SELECT ... FOR UPDATE`` (a no-op on SQLite).

    Everything that changes a case's inputs, creates a run for it or deletes it takes
    this lock first, then re-checks its conditions, so those operations serialise.
    A case deleted meanwhile is 404. Lock order: case row, then owner user row.
    """
    case = db.scalar(
        select(Case)
        .where(Case.id == case_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if case is None or case.deleted_at is not None:
        raise case_not_found()
    return case


def lock_user(db: Session, user_id: UUID) -> User:
    """Lock the user row so that quota checks of concurrent uploads serialise."""
    return db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def case_file(storage: LocalStudyStorage, case_id: UUID, relative: str) -> Path | None:
    """Path of a stored file of the case, or None unless it is a regular file inside
    the case directory reached without a symbolic link."""
    if not relative.startswith(f"{case_id}/"):
        return None
    try:
        storage.resolve_relative(relative)
    except ValueError:
        return None
    base = storage.case_dir(case_id)
    path = storage.root / relative
    current = base
    for part in Path(relative).parts[1:]:
        current = current / part
        if current.is_symlink():
            return None
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
