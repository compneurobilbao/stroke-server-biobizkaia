from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, auth, notifications, shares, studies, users
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for api_module in (admin, auth, notifications, users, studies, shares):
    app.include_router(api_module.router, prefix=settings.api_prefix)
