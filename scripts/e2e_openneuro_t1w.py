#!/usr/bin/env python3
"""End-to-end test of a running instance with a public T1w image from OpenNeuro.

Downloads one defaced T1w image from OpenNeuro dataset ds000001 (licence CC0),
verifies its pinned SHA-256, uploads it through the HTTP API, waits for the
processing job to finish, checks that a PDF report is produced, and deletes the
test study (use --keep to keep it).

Only the Python standard library is used, so it runs on the host or in a
container. Credentials come from the environment, never from arguments:

    E2E_BASE_URL   default http://localhost
    E2E_EMAIL      account used for the test (a dedicated test account)
    E2E_PASSWORD

Exit codes: 0 passed, 1 failed, 2 configuration or download problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DATASET = "ds000001"
SOURCE_URL = "https://s3.amazonaws.com/openneuro.org/ds000001/sub-01/anat/sub-01_T1w.nii.gz"
SOURCE_SHA256 = "bdb7022ae229c5b8edd16425928c9243c562f84082b9b8e6f97cdba8b9354a98"
FILENAME = "sub-01_T1w.nii.gz"
TERMINAL = {"completed", "failed", "canceled"}


def log(message: str) -> None:
    print(f"[e2e] {message}", flush=True)


def fetch_input(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / FILENAME
    if not target.exists():
        log(f"downloading {SOURCE_URL} (OpenNeuro {DATASET}, CC0)")
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
            target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        target.unlink()
        raise SystemExit(f"[e2e] checksum mismatch for {FILENAME}: {digest}")
    log(f"input ok: {target} ({target.stat().st_size} bytes, sha256 verified)")
    return target


class Api:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + "/api"
        self.token: str | None = None

    def _request(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        request = urllib.request.Request(self.base + path, data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read()

    def json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, _, raw = self._request(method, path, body, "application/json" if body else None)
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {"raw": raw[:200].decode(errors="replace")}

    def login(self, email: str, password: str) -> None:
        status, data = self.json("POST", "/auth/login", {"email": email, "password": password})
        if status != 200 or "access_token" not in data:
            raise SystemExit(f"[e2e] login failed ({status}): {data.get('detail', data)}")
        self.token = data["access_token"]

    def upload(self, path: Path) -> dict:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/gzip\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        status, _, raw = self._request("POST", "/studies/upload", body, f"multipart/form-data; boundary={boundary}")
        data = json.loads(raw or b"{}")
        if status != 201:
            raise SystemExit(f"[e2e] upload failed ({status}): {data.get('detail', data)}")
        return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=int, default=7200, help="seconds to wait for processing (default 7200)")
    parser.add_argument("--cache", default=os.environ.get("E2E_CACHE", ".e2e-cache"), help="download cache folder")
    parser.add_argument("--keep", action="store_true", help="keep the test study instead of deleting it")
    args = parser.parse_args()

    email, password = os.environ.get("E2E_EMAIL"), os.environ.get("E2E_PASSWORD")
    if not email or not password:
        log("set E2E_EMAIL and E2E_PASSWORD for a dedicated test account")
        return 2
    try:
        source = fetch_input(Path(args.cache))
    except OSError as error:
        log(f"download failed: {error}")
        return 2

    api = Api(os.environ.get("E2E_BASE_URL", "http://localhost"))
    api.login(email, password)
    log("logged in")
    started = time.monotonic()
    uploaded = api.upload(source)
    study_id = uploaded["id"]
    log(f"uploaded study {study_id} (status {uploaded['status']})")

    status_data: dict = {}
    last = None
    while time.monotonic() - started < args.timeout:
        code, status_data = api.json("GET", f"/studies/{study_id}/status")
        if code != 200:
            log(f"status request failed ({code}): {status_data}")
            return 1
        if status_data["status"] != last:
            last = status_data["status"]
            log(f"status: {last} after {int(time.monotonic() - started)} s")
        if last in TERMINAL:
            break
        time.sleep(15)
    else:
        log(f"timed out after {args.timeout} s in status {last}")
        return 1

    passed = status_data.get("status") == "completed" and status_data.get("has_pdf")
    if passed:
        code, content_type, pdf = api._request("GET", f"/studies/{study_id}/download/pdf")
        passed = code == 200 and pdf.startswith(b"%PDF")
        log(f"report: HTTP {code}, {len(pdf)} bytes, {'valid PDF' if passed else 'NOT a PDF'}")
    else:
        log(f"processing ended as {status_data.get('status')}: {status_data.get('error_message')}")
        _, logs = api.json("GET", f"/studies/{study_id}/logs")
        tail = (logs.get("stderr") or logs.get("log") or json.dumps(logs))[-2000:]
        log("last log lines:\n" + str(tail))
    if status_data.get("processing_warnings"):
        log(f"warnings: {status_data['processing_warnings']}")

    if not args.keep:
        code, _ = api.json("DELETE", f"/studies/{study_id}")
        log(f"test study deleted (HTTP {code})")
    log(f"RESULT: {'PASSED' if passed else 'FAILED'} in {int(time.monotonic() - started)} s")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
