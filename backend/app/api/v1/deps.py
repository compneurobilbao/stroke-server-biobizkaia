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
from dataclasses import dataclass
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
from app.core.config import Settings
from app.models.audit_event import AuditEvent
from app.models.case import Case, DeploymentProfile
from app.models.pipeline_run import LEGACY_PIPELINE_ID, PipelineRun
from app.models.user import User
from app.services.storage import LocalStudyStorage
from orchestrator.bundle_resolver import BundleResolver, RegistryBundleResolver
from orchestrator.catalog import (
    DirectoryPipelineRegistry,
    DirectoryStepCatalog,
    PipelineRegistry,
    StepCatalog,
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


# ------------------------------------------------------------ service providers


@dataclass(frozen=True)
class Orchestration:
    """What the API needs from the orchestrator package (loaded once per process)."""

    pipelines: PipelineRegistry
    steps: StepCatalog
    bundles: BundleResolver
    platform_version: str


@lru_cache(maxsize=1)
def _default_orchestration() -> Orchestration:
    from app.db.session import SessionLocal

    env = os.environ
    return Orchestration(
        pipelines=DirectoryPipelineRegistry(env.get("PIPELINES_DIR", "/app/pipelines")),
        steps=DirectoryStepCatalog(env.get("STEP_MANIFESTS_DIR", "/app/steps")),
        bundles=RegistryBundleResolver(
            env.get("BUNDLE_ROOT", "/app/data/bundles"), SessionLocal
        ),
        platform_version=env.get("PLATFORM_VERSION", "0.0.0-dev"),
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
