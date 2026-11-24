from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    auth_secret_key: str = "change-me-in-production"
    auth_access_token_expire_minutes: int = 480
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
        if self.environment == "development":
            return
        if (
            self.auth_secret_key == "change-me-in-production"
            or len(self.auth_secret_key) < 32
        ):
            raise RuntimeError(
                "AUTH_SECRET_KEY must be set to a custom key outside development"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
