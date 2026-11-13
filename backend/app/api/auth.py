from __future__ import annotations

from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LogoutResponse,
    TokenResponse,
    UserRead,
)
from app.services.audit import record_event
from app.services.auth import (
    client_ip,
    create_access_token,
    get_current_user,
    get_user_by_email,
    normalize_email,
    verify_password,
)
from app.api.helpers import _user_read

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse)
def login(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = get_user_by_email(db, payload.email)
    if (
        not user
        or not user.is_active
        or not verify_password(payload.password, user.hashed_password)
    ):
        record_event(
            db,
            "login_failed",
            details={"email": normalize_email(payload.email)},
            ip_address=client_ip(request),
        )
        db.commit()
        raise api_error(ErrorCode.INVALID_CREDENTIALS)

    user.last_login_at = datetime.utcnow()
    record_event(
        db,
        "login_succeeded",
        actor=user.email,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user, settings),
        user=_user_read(db, user),
    )


@router.post("/auth/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    record_event(
        db,
        "logout",
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return LogoutResponse(message="Logged out")


@router.get("/auth/me", response_model=UserRead)
def current_user(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> UserRead:
    return _user_read(db, current_user)
