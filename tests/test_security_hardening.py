"""Security hardening (card SEC-01): signing key, login throttle, upload cap, wiring."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.cli.forwarded_allow_ips import attached_networks, forwarded_allow_ips
from app.core.config import DEFAULT_AUTH_SECRET_KEY, Settings, get_settings
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.services import auth as auth_service
from app.services.audit import record_event
from app.services.storage import LocalStudyStorage, UploadTooLargeError
from tests.test_api import auth_headers, create_test_user

STRONG_KEY = "s" * 64


# ---------------------------------------------------------------- signing key


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
@pytest.mark.parametrize("key", [DEFAULT_AUTH_SECRET_KEY, "short-key", "x" * 31])
def test_weak_signing_key_is_refused_in_every_environment(environment, key):
    settings = Settings(
        environment=environment, auth_secret_key=key, allow_insecure_dev_key=False
    )
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
        settings.validate_api_auth_settings()


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_custom_signing_key_is_accepted_in_every_environment(environment):
    Settings(
        environment=environment, auth_secret_key=STRONG_KEY, allow_insecure_dev_key=False
    ).validate_api_auth_settings()
    Settings(
        environment=environment, auth_secret_key="x" * 32, allow_insecure_dev_key=False
    ).validate_api_auth_settings()


def test_insecure_key_needs_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET_KEY", DEFAULT_AUTH_SECRET_KEY)
    monkeypatch.setenv("ALLOW_INSECURE_DEV_KEY", "0")
    with pytest.raises(RuntimeError):
        Settings().validate_api_auth_settings()
    monkeypatch.setenv("ALLOW_INSECURE_DEV_KEY", "1")
    Settings().validate_api_auth_settings()


def test_empty_compose_values_fall_back_to_the_defaults(monkeypatch):
    # docker-compose.yml forwards unset variables as empty strings.
    for name in (
        "ALLOW_INSECURE_DEV_KEY",
        "LOGIN_MAX_FAILED_ATTEMPTS",
        "LOGIN_FAILURE_WINDOW_MINUTES",
    ):
        monkeypatch.setenv(name, "")
    settings = Settings()
    assert settings.allow_insecure_dev_key is False
    assert settings.login_max_failed_attempts == 5
    assert settings.login_failure_window_minutes == 15


# ---------------------------------------------------------------- login throttle


WRONG = "wrong-pass"


def _login(client, email, attempt=WRONG):
    return client.post("/api/auth/login", json={"email": email, "password": attempt})


def _limit_env(monkeypatch, attempts=3, minutes=15, per_account=50):
    monkeypatch.setenv("LOGIN_MAX_FAILED_ATTEMPTS", str(attempts))
    monkeypatch.setenv("LOGIN_MAX_FAILED_ATTEMPTS_PER_ACCOUNT", str(per_account))
    monkeypatch.setenv("LOGIN_FAILURE_WINDOW_MINUTES", str(minutes))
    get_settings.cache_clear()


def test_account_is_throttled_after_repeated_failures(client, monkeypatch):
    _limit_env(monkeypatch, attempts=3)
    create_test_user(client, "alice@example.org", "right-pass")

    for _ in range(3):
        assert _login(client, "alice@example.org").status_code == 401

    # The correct password is refused too while the account is throttled.
    blocked = _login(client, "alice@example.org", "right-pass")
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_login_attempts"
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= 15 * 60 + 1

    # Other accounts are not affected.
    create_test_user(client, "bob@example.org", "bob-pass")
    assert _login(client, "bob@example.org", "bob-pass").status_code == 200

    db = client.app.state.testing_session_local()
    types = [e.event_type for e in db.query(AuditEvent).order_by(AuditEvent.timestamp)]
    db.close()
    assert types.count("login_failed") == 3
    assert types.count("login_throttled") == 1


def test_unknown_address_is_throttled_like_an_existing_one(client, monkeypatch):
    _limit_env(monkeypatch, attempts=2)
    for _ in range(2):
        assert _login(client, "nobody@example.org").status_code == 401
    assert _login(client, "Nobody@Example.org").status_code == 429


def test_failures_age_out_of_the_window(client, monkeypatch):
    _limit_env(monkeypatch, attempts=2, minutes=15)
    create_test_user(client, "alice@example.org", "right-pass")
    for _ in range(2):
        _login(client, "alice@example.org")
    assert _login(client, "alice@example.org", "right-pass").status_code == 429

    db = client.app.state.testing_session_local()
    for event in db.query(AuditEvent).filter(AuditEvent.event_type == "login_failed"):
        event.timestamp = datetime.utcnow() - timedelta(minutes=16)
    db.commit()
    db.close()

    assert _login(client, "alice@example.org", "right-pass").status_code == 200


def test_successful_login_resets_the_count(client, monkeypatch):
    _limit_env(monkeypatch, attempts=3)
    create_test_user(client, "alice@example.org", "right-pass")
    for _ in range(2):
        _login(client, "alice@example.org")
    assert _login(client, "alice@example.org", "right-pass").status_code == 200
    # Failures before the success no longer count: two more are allowed.
    for _ in range(2):
        assert _login(client, "alice@example.org").status_code == 401
    assert _login(client, "alice@example.org", "right-pass").status_code == 200


def test_throttle_can_be_disabled(client, monkeypatch):
    _limit_env(monkeypatch, attempts=0, per_account=0)
    for _ in range(8):
        assert _login(client, "alice@example.org").status_code == 401


def _record_failures(client, email, addresses):
    db = client.app.state.testing_session_local()
    for address in addresses:
        record_event(
            db,
            auth_service.LOGIN_FAILED_EVENT,
            details=auth_service.login_failure_details(email),
            ip_address=address,
        )
    db.commit()
    db.close()


def _retry_after(client, email, address):
    db = client.app.state.testing_session_local()
    try:
        user = auth_service.get_user_by_email(db, email)
        return auth_service.login_retry_after_seconds(
            db, email, user, get_settings(), address
        )
    finally:
        db.close()


def test_failures_from_one_address_do_not_lock_out_other_addresses(client, monkeypatch):
    _limit_env(monkeypatch, attempts=3, per_account=50)
    create_test_user(client, "admin@example.org", "right-pass")
    _record_failures(client, "admin@example.org", ["203.0.113.7"] * 3)

    assert _retry_after(client, "admin@example.org", "203.0.113.7") is not None
    # The owner, from another address, can still sign in.
    assert _retry_after(client, "admin@example.org", "198.51.100.1") is None
    assert _login(client, "admin@example.org", "right-pass").status_code == 200


def test_per_account_ceiling_applies_over_all_addresses(client, monkeypatch):
    _limit_env(monkeypatch, attempts=3, per_account=4)
    addresses = [f"203.0.113.{i}" for i in range(4)]  # one failure each, under 3 per address
    _record_failures(client, "admin@example.org", addresses)

    assert _retry_after(client, "admin@example.org", "198.51.100.1") is not None
    assert _login(client, "Admin@example.org").status_code == 429


def test_per_account_ceiling_can_be_disabled(client, monkeypatch):
    _limit_env(monkeypatch, attempts=3, per_account=0)
    _record_failures(client, "admin@example.org", [f"203.0.113.{i}" for i in range(10)])
    assert _retry_after(client, "admin@example.org", "198.51.100.1") is None


def test_login_hashes_a_dummy_password_for_unknown_and_inactive_accounts(
    client, monkeypatch
):
    calls: list[str] = []
    real_verify = auth_service.verify_password

    def counting_verify(password, hashed):
        calls.append(hashed)
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_service, "verify_password", counting_verify)
    dummy = auth_service._dummy_password_hash()

    assert _login(client, "nobody@example.org").status_code == 401
    assert calls == [dummy]

    user_id = create_test_user(client, "inactive@example.org", "right-pass")
    db = client.app.state.testing_session_local()
    user = db.get(User, user_id)
    user.is_active = False
    db.commit()
    db.close()
    calls.clear()
    assert _login(client, "inactive@example.org", "right-pass").status_code == 401
    assert len(calls) == 1 and calls[0] != dummy  # real hash checked, still refused

    calls.clear()
    headers, _ = auth_headers(client, "alice@example.org", "right-pass")
    assert len(calls) == 1 and calls[0] != dummy


def test_check_login_password_paths():
    hashed = auth_service.hash_password("pw")
    active = User(email="a@example.org", hashed_password=hashed, is_active=True)
    inactive = User(email="b@example.org", hashed_password=hashed, is_active=False)
    no_password = User(email="c@example.org", hashed_password=None, is_active=True)
    assert auth_service.check_login_password("pw", active) is True
    assert auth_service.check_login_password("bad", active) is False
    assert auth_service.check_login_password("pw", inactive) is False
    assert auth_service.check_login_password("pw", no_password) is False
    assert auth_service.check_login_password("pw", None) is False


# ---------------------------------------------------------------- legacy upload cap


class _CountingStream(io.RawIOBase):
    """Endless stream of 1 MiB chunks that records how many were read."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        return b"\0" * (size if size and size > 0 else 1024 * 1024)


class _Upload:
    def __init__(self, stream) -> None:
        self.file = stream


def test_save_upload_stops_at_the_limit_and_removes_the_partial_file(tmp_path):
    storage = LocalStudyStorage(tmp_path)
    study_id = uuid4()
    stream = _CountingStream()

    with pytest.raises(UploadTooLargeError):
        storage.save_upload(study_id, _Upload(stream), "big.nii", max_bytes=3 * 1024 * 1024)

    assert stream.reads == 4  # aborted on the first chunk over the limit
    assert not (storage.input_original_dir(study_id) / "big.nii").exists()


def test_save_upload_without_limit_keeps_the_file(tmp_path):
    storage = LocalStudyStorage(tmp_path)
    study_id = uuid4()
    path, size, _ = storage.save_upload(study_id, _Upload(io.BytesIO(b"abc")), "a.nii")
    assert size == 3 and path.read_bytes() == b"abc"


def test_legacy_upload_over_the_limit_returns_413_and_leaves_nothing(
    client, monkeypatch
):
    headers, _ = auth_headers(client, "alice@example.org", "right-pass")
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
    get_settings.cache_clear()

    response = client.post(
        "/api/studies/upload",
        files={"file": ("big.nii", b"\0" * (1024 * 1024 + 1), "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
    root = get_settings().storage_root
    assert not root.exists() or not any(root.iterdir())
    assert client.get("/api/studies", headers=headers).json() == []


# ---------------------------------------------------------------- deployment wiring

ROOT = Path(__file__).resolve().parent.parent


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]


def test_published_ports_are_bound_to_loopback():
    services = {**_compose("docker-compose.yml")}
    override = _compose("docker-compose.override.yml")
    ports = [p for svc in override.values() for p in svc.get("ports", [])]
    ports += services["mailpit"]["ports"]
    assert ports and all(str(p).startswith("127.0.0.1:") for p in ports), ports


def test_api_trusts_proxy_headers_only_from_its_network():
    command = " ".join(_compose("docker-compose.yml")["api"]["command"])
    assert "--proxy-headers" in command
    assert "--forwarded-allow-ips" in command
    assert "'*'" not in command and '"*"' not in command
    assert "app.cli.forwarded_allow_ips" in command


def test_redis_password_reaches_redis_and_every_redis_client():
    services = _compose("docker-compose.yml")
    assert "--requirepass" in " ".join(services["redis"]["command"])
    for name in ("redis", "api", "worker", "pipeline-worker"):
        assert services[name]["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:-}"


REDIS_PW = "p@ss/word"  # reserved URL characters are percent-encoded
OTHER_PW = "x"


def test_redis_password_is_added_to_redis_urls_only():
    urls = {
        "celery_broker_url": "redis://redis:6379/0",
        "celery_result_backend": "rediss://redis:6380/1",
    }
    settings = Settings(redis_password=REDIS_PW, **urls)
    assert settings.celery_broker_url == "redis://:p%40ss%2Fword@redis:6379/0"
    assert settings.celery_result_backend == "rediss://:p%40ss%2Fword@redis:6380/1"
    assert Settings(redis_password="", **urls).celery_broker_url == urls["celery_broker_url"]
    memory = Settings(redis_password=OTHER_PW, celery_broker_url="memory://")
    assert memory.celery_broker_url == "memory://"
    explicit = "redis://:other@redis:6379/0"
    assert (
        Settings(redis_password=OTHER_PW, celery_broker_url=explicit).celery_broker_url
        == explicit
    )


ROUTES = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "eth1\t0010A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"
    "lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0\n"
)


def test_celery_app_uses_the_password_even_with_celery_env_urls():
    """Celery reads CELERY_BROKER_URL / CELERY_RESULT_BACKEND from the environment before
    its constructor arguments; the app must still carry REDIS_PASSWORD."""
    env = {
        **os.environ,
        "CELERY_BROKER_URL": "redis://redis:6379/0",
        "CELERY_RESULT_BACKEND": "redis://redis:6379/1",
        "REDIS_PASSWORD": REDIS_PW,
        "PYTHONPATH": os.pathsep.join(
            [str(ROOT), str(ROOT / "backend"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    code = (
        "import json; from worker.celery_app import celery_app as a; "
        "print(json.dumps([a.conf.broker_url, a.conf.result_backend]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    broker, backend = json.loads(out.strip().splitlines()[-1])
    assert broker == "redis://:p%40ss%2Fword@redis:6379/0"
    assert backend == "redis://:p%40ss%2Fword@redis:6379/1"


def test_forwarded_allow_ips_lists_the_attached_networks(tmp_path):
    assert attached_networks(ROUTES) == ["172.18.0.0/16", "192.168.16.0/20"]
    table = tmp_path / "route"
    table.write_text(ROUTES, encoding="ascii")
    assert forwarded_allow_ips(table) == "172.18.0.0/16,192.168.16.0/20"
    assert forwarded_allow_ips(tmp_path / "missing") == "127.0.0.1"
    table.write_text(ROUTES.splitlines()[0] + "\n", encoding="ascii")
    assert forwarded_allow_ips(table) == "127.0.0.1"


def test_reverse_proxy_limits_login_and_sets_security_headers():
    conf = (ROOT / "infra" / "reverse-proxy" / "nginx.conf").read_text(encoding="utf-8")
    assert "limit_req_zone $binary_remote_addr zone=login" in conf
    login = conf.split("location = /api/auth/login", 1)[1].split("}", 1)[0]
    assert "limit_req zone=login" in login
    assert "error_page 429 = @login_throttled" in login
    throttled = conf.split("location @login_throttled", 1)[1]
    assert "application/json" in throttled and '"code": "too_many_login_attempts"' in throttled
    # X-Forwarded-For is overwritten with the address nginx sees, never appended to.
    assert "$proxy_add_x_forwarded_for" not in conf
    assert conf.count("proxy_set_header X-Forwarded-For $remote_addr;") == conf.count(
        "proxy_pass "
    )
    for header in (
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "DENY"',
        "Referrer-Policy",
        "Content-Security-Policy",
    ):
        assert f"add_header {header}" in conf, header
    # add_header inside a location would drop the server-level headers there.
    for block in conf.split("\n    location ")[1:]:
        assert "add_header" not in block.split("}", 1)[0]
