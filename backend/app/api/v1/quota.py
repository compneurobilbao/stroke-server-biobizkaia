"""Storage quota of a user (docs/data-model.md section 12.4, owner decision 4).

Usage is computed by ``app.services.quota.storage_used``: every input file and every
stored artefact of the user's live cases, the same figure the legacy endpoints and the
users endpoint report since the legacy cut-over (migration 0012).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.api.v1.errors import v1_error
from app.models.user import User
from app.services.quota import storage_used

__all__ = ["check_quota", "storage_used"]


def check_quota(
    db: Session, owner: User, additional_bytes: int, replaced_bytes: int = 0
) -> None:
    """Refuse (413) when ``additional_bytes`` would exceed the owner's quota."""
    if owner.storage_quota_bytes is None:
        return
    used = storage_used(db, owner.id) - replaced_bytes
    if used + additional_bytes > owner.storage_quota_bytes:
        raise v1_error(
            413,
            "storage_quota_exceeded",
            "The operation exceeds the storage quota of the case owner",
        )
