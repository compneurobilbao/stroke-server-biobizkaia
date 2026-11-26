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
    LOGIN_FAILED_EVENT,
    LOGIN_THROTTLED_EVENT,
    check_login_password,
    client_ip,
    create_access_token,
    get_current_user,
    get_user_by_email,
    login_failure_details,
    login_retry_after_seconds,
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
    retry_after = login_retry_after_seconds(db, payload.email, user, settings)
    if retry_after is not None:
        # Checked before the password, so a correct guess during the lock is refused too.
        record_event(
            db,
            LOGIN_THROTTLED_EVENT,
            details=login_failure_details(payload.email),
            ip_address=client_ip(request),
        )
        db.commit()
        raise api_error(
            ErrorCode.TOO_MANY_LOGIN_ATTEMPTS,
            headers={"Retry-After": str(retry_after)},
        )
    if not check_login_password(payload.password, user):
        record_event(
            db,
            LOGIN_FAILED_EVENT,
            details=login_failure_details(payload.email),
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
