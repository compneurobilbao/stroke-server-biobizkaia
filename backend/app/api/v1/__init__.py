"""Versioned case and run API, mounted at ``/api/v1`` (card F0-30)."""

from fastapi import APIRouter

from app.api.v1 import admin, cases, openapi, pipelines, plans, runs
from app.api.v1.deps import effective_run_plan_policy

router = APIRouter(prefix="/v1")
for _module in (pipelines, plans, cases, runs, admin, openapi):
    router.include_router(_module.router)

# Refuse to start with a RUN_PLAN_POLICY that its deployment profile does not allow
# (run-plans.md 3.5): this module is imported when the application starts.
effective_run_plan_policy()
