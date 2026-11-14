"""OpenAPI document of the v1 API alone, served at ``/api/v1/openapi.json`` and ``/api/v1/docs``.

A snapshot is committed as ``docs/api/openapi-v1.json`` and a test checks it is current.
Regenerate it with ``PYTHONPATH=.:backend python -m app.api.v1.openapi docs/api/openapi-v1.json``.
"""

from __future__ import annotations

import json
import sys

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

API_V1_VERSION = "1.0.0"
router = APIRouter()


def build_openapi(app: FastAPI) -> dict:
    prefix = f"{app.openapi_url.rsplit('/', 1)[0]}/v1/"
    routes = [
        r
        for r in app.routes
        if getattr(r, "path", "").startswith(prefix)
        and getattr(r, "include_in_schema", False)
    ]
    return get_openapi(
        title="Stroke Server API v1",
        version=API_V1_VERSION,
        description="Cases, pipelines, runs and artefacts. Research use only.",
        routes=routes,
    )


@router.get("/openapi.json", include_in_schema=False)
def openapi_json(request: Request) -> dict:
    return build_openapi(request.app)


@router.get("/docs", include_in_schema=False)
def docs(request: Request):
    return get_swagger_ui_html(
        openapi_url=str(request.url_for("openapi_json")), title="Stroke Server API v1"
    )


if __name__ == "__main__":  # pragma: no cover
    from app.main import app

    text = json.dumps(build_openapi(app), indent=2, sort_keys=True) + "\n"
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
