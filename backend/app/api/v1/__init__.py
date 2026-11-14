"""Versioned case and run API, mounted at ``/api/v1`` (card F0-30)."""

from fastapi import APIRouter

from app.api.v1 import admin, cases, openapi, pipelines, runs

router = APIRouter(prefix="/v1")
for _module in (pipelines, cases, runs, admin, openapi):
    router.include_router(_module.router)
