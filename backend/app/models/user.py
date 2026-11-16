from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


SYSTEM_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class UserRole(str, enum.Enum):
    admin = "admin"
    researcher = "researcher"


# Roles accepted by the CHECK on users.role. The API assigns only admin and researcher
# until F1-03 enforces the others (docs/data-model.md section 6).
RESERVED_ROLES = ("reviewer", "coordinator", "service")
ALL_ROLES = (UserRole.admin.value, UserRole.researcher.value, *RESERVED_ROLES)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{r}'" for r in ALL_ROLES) + ")",
            name="ck_users_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(
        String(50), default=UserRole.researcher.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    notify_on_processing_completed: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    notify_on_processing_failed: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    studies = relationship("Study", back_populates="owner")
    audit_events = relationship("AuditEvent", back_populates="actor_user")
    share_links = relationship("ShareLink", back_populates="created_by")
    notifications = relationship("Notification", back_populates="recipient")

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.admin.value
