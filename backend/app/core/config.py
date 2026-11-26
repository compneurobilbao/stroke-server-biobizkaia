from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Who may start a run in free mode (docs/design/run-plans.md 3.5). Roles are those of
# ``users.role``; presets and the full plan are open to everyone allowed to create runs.
RUN_PLAN_POLICIES: dict[str, frozenset[str]] = {
    "presets_only": frozenset(),
    "free_for_admins": frozenset({"admin"}),
    "free_for_researchers": frozenset({"admin", "researcher"}),
}
# Profiles whose policy is fixed to ``presets_only``: a configuration that sets another
# value is refused at start. The other profiles take the value of ``RUN_PLAN_POLICY``,
# or ``free_for_researchers`` when it is not set.
PRESETS_ONLY_PROFILES = frozenset({"research-public", "mask-only"})
DEFAULT_RUN_PLAN_POLICY = "free_for_researchers"


def run_plan_policy(profile: str, env: Mapping[str, str] | None = None) -> str:
    """The effective ``RUN_PLAN_POLICY`` of a deployment profile.

    ``RUN_PLAN_POLICY`` is read from the process environment (an empty value counts as
    unset). On ``research-public`` and ``mask-only`` the policy is ``presets_only`` and
    any other value raises ``RuntimeError``; elsewhere an unset value is
    ``free_for_researchers``. An unknown value always raises.
    """
    env = os.environ if env is None else env
    value = (env.get("RUN_PLAN_POLICY") or "").strip()
    if value and value not in RUN_PLAN_POLICIES:
        raise RuntimeError(f"RUN_PLAN_POLICY must be one of {sorted(RUN_PLAN_POLICIES)}")
    if profile in PRESETS_ONLY_PROFILES:
        if value not in ("", "presets_only"):
            raise RuntimeError(
                f"RUN_PLAN_POLICY must be presets_only on the {profile} profile "
                f"(got {value})"
            )
        return "presets_only"
    return value or DEFAULT_RUN_PLAN_POLICY


# Placeholder signing key shipped in the examples and the Compose defaults. The API refuses
# it, and any key shorter than MIN_AUTH_SECRET_KEY_LENGTH, unless ALLOW_INSECURE_DEV_KEY is
# set (local development and tests only). ENVIRONMENT plays no part in the check.
DEFAULT_AUTH_SECRET_KEY = "change-me-in-production"
MIN_AUTH_SECRET_KEY_LENGTH = 32


def with_redis_password(url: str, password: str) -> str:
    """``url`` with ``password`` added, for a ``redis://`` or ``rediss://`` URL without
    credentials; any other URL (or an empty password) is returned unchanged."""
    if not password:
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("redis", "rediss") or "@" in parts.netloc:
        return url
    netloc = f":{quote(password, safe='')}@{parts.netloc}"
    return urlunsplit(parts._replace(netloc=netloc))


def free_mode_allowed(policy: str, role: str) -> bool:
    """True if a user with ``role`` may use free mode under ``policy``."""
    return role in RUN_PLAN_POLICIES[policy]


class Settings(BaseSettings):
    app_name: str = "Stroke Server"
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = Field(
        default="postgresql+psycopg://neuroimagen:neuroimagen@postgres:5432/neuroimagen"
    )
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    # Optional Redis password (requirepass). When set, it is added to the two Redis URLs
    # above unless they already carry credentials. Empty = Redis without a password.
    redis_password: str = ""
    storage_root: Path = Path("/app/data/studies")
    allowed_extensions: str = ".nii,.nii.gz,.dcm,.zip,.tar,.tar.gz,.gz,.json,.txt"
    max_upload_size_mb: int = 1024
    processor_command: str = (
        "python /app/external_processor/dummy_processor.py "
        "--input {input_dir} --output {output_dir} --study-id {study_id}"
    )
    processor_backend: str = "dummy"
    processor_name: str = "dummy-development-processor"
    processor_version: str | None = "0.1.0"
    compneuro_container_image: str = "compneurobilbaolab/compneuro-anatproc:1.1"
    compneuro_project_mount: Path = Path("/project")
    compneuro_command: str = "bash /app/src/apreproc_launcher.sh"
    processing_timeout_seconds: int = 0
    max_concurrent_processing_jobs: int = 1
    # Recovery of legacy attempts left without a live worker task (worker/legacy_recovery.py).
    legacy_recovery_queued_minutes: int = 30
    legacy_recovery_running_hours: int = 6
    generate_output_zip: bool = True
    generate_technical_pdf: bool = True
    generate_rendered_png: bool = True
    nifti_renderer: str = "slicer"
    nifti_render_max_files: int = 50
    nifti_render_timeout_seconds: int = 300
    technical_report_filename: str = "technical_report.pdf"
    bids_validate: bool = False
    cors_origins: str = "http://localhost,http://localhost:5173"
    auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
    # Explicit opt-in that lets the API start with the placeholder or a short signing key.
    # Local development and the test suite only; never on a shared instance.
    allow_insecure_dev_key: bool = False
    auth_access_token_expire_minutes: int = 480
    # Per-account sign-in throttle (services/auth.py): after this many failed logins for
    # one e-mail address within the window, further attempts get 429 until the oldest
    # counted failure leaves the window. A successful login resets the count. 0 = off.
    login_max_failed_attempts: int = 5
    login_failure_window_minutes: int = 15
    share_link_expire_hours: int = 72
    retention_enabled: bool = False
    retention_days: int = 90
    app_public_base_url: str = "http://localhost"
    notifications_email_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    smtp_use_tls: bool = True
    # Opt-in synthetic example inputs (card MVP-03): a directory with manifest.json and the
    # example files. Empty = off. Never point it at real data.
    example_inputs_dir: str = ""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @model_validator(mode="after")
    def _apply_redis_password(self) -> "Settings":
        self.celery_broker_url = with_redis_password(
            self.celery_broker_url, self.redis_password
        )
        self.celery_result_backend = with_redis_password(
            self.celery_result_backend, self.redis_password
        )
        return self

    @property
    def allowed_extension_list(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.allowed_extensions.split(",")
            if item.strip()
        ]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def validate_api_auth_settings(self) -> None:
        """Refuse a weak token signing key, whatever ``ENVIRONMENT`` says.

        Weak means the shipped placeholder or fewer than ``MIN_AUTH_SECRET_KEY_LENGTH``
        characters. ``ALLOW_INSECURE_DEV_KEY`` is the only way to start with one.
        """
        weak = (
            self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY
            or len(self.auth_secret_key) < MIN_AUTH_SECRET_KEY_LENGTH
        )
        if not weak:
            return
        if self.allow_insecure_dev_key:
            log.warning(
                "ALLOW_INSECURE_DEV_KEY is set: tokens are signed with a weak key. "
                "Never use this on a shared instance."
            )
            return
        raise RuntimeError(
            "AUTH_SECRET_KEY must be a custom key of at least "
            f"{MIN_AUTH_SECRET_KEY_LENGTH} characters (for example the output of "
            "`openssl rand -hex 32`). ALLOW_INSECURE_DEV_KEY=1 accepts a weak key for "
            "local development and tests only."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
