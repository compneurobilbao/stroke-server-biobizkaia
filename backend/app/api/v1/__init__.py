"""Versioned case and run API, mounted at ``/api/v1`` (card F0-30)."""

from fastapi import APIRouter

from app.api.v1 import admin, cases, examples, openapi, pipelines, plans, runs
from app.api.v1.deps import effective_min_validation, effective_run_plan_policy

router = APIRouter(prefix="/v1")
for _module in (pipelines, plans, cases, examples, runs, admin, openapi):
    router.include_router(_module.router)

# Refuse to start with a RUN_PLAN_POLICY that its deployment profile does not allow
# (run-plans.md 3.5): this module is imported when the application starts.
effective_run_plan_policy()

# Same for MIN_IMPLEMENTATION_VALIDATION (run-plans.md 4.4): a value below `validated`
# on a public profile, or an unknown level, stops the application at start.
effective_min_validation()
