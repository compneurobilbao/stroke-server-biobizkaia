from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.audit_event import AuditEvent
from app.models.user import User, UserRole

PASSWORD_ITERATIONS = 390_000
PASSWORD_ALGORITHM = "pbkdf2_sha256"
TOKEN_ALGORITHM = "HS256"
LOGIN_FAILED_EVENT = "login_failed"
LOGIN_THROTTLED_EVENT = "login_throttled"

bearer_scheme = HTTPBearer(auto_error=False)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            _b64encode(salt),
            _b64encode(digest),
        ]
    )


def verify_password(password: str, hashed_password: str | None) -> bool:
    if not hashed_password:
        return False
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = hashed_password.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected = _b64decode(digest_raw)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """A hash of a random password, computed once per process."""
    return hash_password(secrets.token_urlsafe(32))


def check_login_password(password: str, user: User | None) -> bool:
    """True only for an existing, active user whose password matches.

    Exactly one PBKDF2 derivation runs on every path, with the user's own hash or with a
    dummy one when the address is unknown, the account is inactive or has no password, so
    the response time does not reveal whether an account exists.
    """
    hashed = user.hashed_password if user is not None else None
    matches = verify_password(password, hashed or _dummy_password_hash())
    return bool(matches and hashed and user is not None and user.is_active)


def login_failure_details(email: str) -> dict:
    """``details`` of a ``login_failed`` audit event; also the key of the throttle."""
    return {"email": normalize_email(email)}


def lock_login_attempts(db: Session, email: str) -> None:
    """Serialise login attempts for one address until the transaction ends (PostgreSQL).

    Without it, concurrent attempts could all read the count before any of them records
    its failure and so exceed the limit. A transaction-scoped advisory lock keyed on the
    normalised address; nothing on other databases (SQLite in tests and development).
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"login:{normalize_email(email)}"},
    )


def login_retry_after_seconds(
    db: Session,
    email: str,
    user: User | None,
    settings: Settings,
    ip_address: str | None,
) -> int | None:
    """Seconds until the next login attempt for ``email`` from ``ip_address`` is allowed,
    or ``None`` when it is allowed now.

    Failures are the ``login_failed`` audit events of the address within the last
    ``LOGIN_FAILURE_WINDOW_MINUTES``, ignoring those before the user's last successful
    login, so the count is shared by every API process and survives restarts. Two limits:

    * ``LOGIN_MAX_FAILED_ATTEMPTS`` per address and client address. Someone guessing from
      one place is stopped, while the owner, from another place, can still sign in.
    * ``LOGIN_MAX_FAILED_ATTEMPTS_PER_ACCOUNT`` per address over all client addresses, a
      much higher ceiling against guessing spread over many client addresses. Reaching it
      locks the account for everyone until failures age out: the price of the ceiling.

    Unknown addresses are counted the same way, so a 429 says nothing about whether an
    account exists. Refused attempts are recorded as ``login_throttled`` and do not
    count, so a lock lifts when the oldest counted failure leaves the window.
    """
    window = timedelta(minutes=max(settings.login_failure_window_minutes, 1))
    now = datetime.utcnow()
    since = now - window
    if user is not None and user.last_login_at and user.last_login_at > since:
        since = user.last_login_at
    details = json.dumps(login_failure_details(email), ensure_ascii=False)
    base = select(func.count(AuditEvent.id), func.min(AuditEvent.timestamp)).where(
        AuditEvent.event_type == LOGIN_FAILED_EVENT,
        AuditEvent.timestamp > since,
        AuditEvent.details == details,
    )
    same_client = (
        AuditEvent.ip_address.is_(None)
        if ip_address is None
        else AuditEvent.ip_address == ip_address
    )
    checks = (
        (settings.login_max_failed_attempts, base.where(same_client)),
        (settings.login_max_failed_attempts_per_account, base),
    )
    waits = []
    for limit, query in checks:
        if limit <= 0:
            continue
        count, oldest = db.execute(query).one()
        if count >= limit:
            # With more failures than the limit, waiting for the oldest one is a lower
            # bound; the next attempt is checked again.
            waits.append(max(int((oldest + window - now).total_seconds()) + 1, 1))
    return max(waits) if waits else None


def create_access_token(user: User, settings: Settings) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.auth_access_token_expire_minutes
    )
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "exp": int(expires_at.timestamp()),
    }
    header = {"alg": TOKEN_ALGORITHM, "typ": "JWT"}
    signing_input = f"{_b64json(header)}.{_b64json(payload)}"
    signature = _sign(signing_input, settings.auth_secret_key)
    return f"{signing_input}.{signature}"


def decode_access_token(token: str, settings: Settings) -> dict:
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
        signing_input = f"{header_raw}.{payload_raw}"
        expected_signature = _sign(signing_input, settings.auth_secret_key)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("invalid signature")
        header = json.loads(_b64decode(header_raw))
        payload = json.loads(_b64decode(payload_raw))
        if header.get("alg") != TOKEN_ALGORITHM:
            raise ValueError("invalid algorithm")
        if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("expired token")
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        raise api_error(ErrorCode.NOT_AUTHENTICATED) from None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise api_error(ErrorCode.NOT_AUTHENTICATED)
    payload = decode_access_token(credentials.credentials, settings)
    user_id = payload.get("sub")
    try:
        user_uuid = UUID(user_id)
    except (TypeError, ValueError):
        raise api_error(ErrorCode.NOT_AUTHENTICATED) from None
    user = db.get(User, user_uuid)
    if not user or not user.is_active or user.deleted_at:
        raise api_error(ErrorCode.NOT_AUTHENTICATED)
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.admin.value:
        raise api_error(ErrorCode.PERMISSION_DENIED)
    return current_user


def require_study_access(study_owner_id: UUID, current_user: User) -> None:
    if current_user.role == UserRole.admin.value:
        return
    if study_owner_id == current_user.id:
        return
    raise api_error(ErrorCode.STUDY_NOT_FOUND)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _sign(signing_input: str, secret_key: str) -> str:
    signature = hmac.new(
        secret_key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).digest()
    return _b64encode(signature)


def _b64json(payload: dict) -> str:
    return _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(
        select(User).where(User.email == normalize_email(email), User.deleted_at.is_(None))
    )
