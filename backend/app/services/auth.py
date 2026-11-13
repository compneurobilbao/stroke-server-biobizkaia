from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.user import User, UserRole

PASSWORD_ITERATIONS = 390_000
PASSWORD_ALGORITHM = "pbkdf2_sha256"
TOKEN_ALGORITHM = "HS256"

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
