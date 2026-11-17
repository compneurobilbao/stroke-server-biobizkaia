from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Nullable since migration 0012 (legacy cut-over): links of cases that have no
    # legacy study have none. Dropped by migration 0014.
    study_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("studies.id"), index=True
    )
    # NOT NULL since migration 0012; the legacy share endpoints address links by case.
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id"), index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime)
    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    study = relationship("Study", back_populates="share_links")
    created_by = relationship("User", back_populates="share_links")
