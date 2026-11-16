import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, auth, notifications, shares, studies, users
from app.api import v1
from app.api.v1.errors import V1Error, v1_error_handler
from app.core.config import get_settings
from app.core.errors import ApiError, api_error_handler

settings = get_settings()
settings.validate_api_auth_settings()

app = FastAPI(
    title=settings.app_name,
    description="API for asynchronous neuroimaging processing and PDF report generation.",
    version="0.1.0",
    openapi_url=f"{settings.api_prefix}/openapi.json",
    docs_url=f"{settings.api_prefix}/docs",
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(V1Error, v1_error_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for api_module in (admin, auth, notifications, users, studies, shares):
    app.include_router(api_module.router, prefix=settings.api_prefix)
app.include_router(v1.router, prefix=settings.api_prefix)


# Legacy endpoints are aliases over the case model for one release (docs/data-model.md
# 10.4); they announce their deprecation and, where one exists, their successor.
_LEGACY_PATHS = re.compile(
    rf"^{re.escape(settings.api_prefix)}/(studies(/|$)|share/|admin/dashboard$)"
)
_LEGACY_STUDY = re.compile(
    rf"^{re.escape(settings.api_prefix)}/studies/([0-9a-fA-F-]{{36}})(/|$)"
)


def legacy_successor(path: str) -> str | None:
    if not path.startswith(f"{settings.api_prefix}/studies"):
        return None
    match = _LEGACY_STUDY.match(path)
    if match:
        return f"{settings.api_prefix}/v1/cases/{match.group(1)}"
    return f"{settings.api_prefix}/v1/cases"


@app.middleware("http")
async def legacy_deprecation_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if _LEGACY_PATHS.match(path):
        response.headers["Deprecation"] = "true"
        successor = legacy_successor(path)
        if successor:
            response.headers["Link"] = f'<{successor}>; rel="successor-version"'
    return response
