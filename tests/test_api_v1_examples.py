"""Opt-in synthetic example inputs (card MVP-03). All data is synthetic bytes."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import AuditEvent, InputFile, User
from tests.test_api_v1 import V1Env, nifti_gz

EXAMPLE_ORIGIN = {
    "kind": "external-tool",
    "tool": "stroke-server-example",
    "version": "ex-left",
}


def manifest(files: list[dict]) -> str:
    return json.dumps({"version": 1, "examples": files})


@pytest.fixture()
def example_dir(tmp_path):
    root = tmp_path / "examples"
    root.mkdir()
    (root / "left.nii.gz").write_bytes(nifti_gz(b"left"))
    (root / "right.nii.gz").write_bytes(nifti_gz(b"right"))
    (root / "manifest.json").write_text(
        manifest(
            [
                {
                    "id": "ex-left",
                    "role": "lesion_mni",
                    "label": "Left",
                    "description": "Synthetic.",
                    "licence": "CC0-1.0",
                    "file": "left.nii.gz",
                },
                {
                    "id": "ex-right",
                    "role": "lesion_mni",
                    "label": "Right",
                    "description": "Synthetic.",
                    "licence": "CC0-1.0",
                    "file": "right.nii.gz",
                },
                {  # a path in `file` is refused: the entry is skipped
                    "id": "ex-bad",
                    "role": "lesion_mni",
                    "label": "Bad",
                    "description": "x",
                    "licence": "CC0-1.0",
                    "file": "../manifest.json",
                },
            ]
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def v1(client):
    yield V1Env(client)


@pytest.fixture()
def v1_on(client, example_dir, monkeypatch):
    monkeypatch.setenv("EXAMPLE_INPUTS_DIR", str(example_dir))
    get_settings.cache_clear()
    yield V1Env(client)


def attach(v1, case_id, role="lesion_mni", example_id="ex-left", headers=None):
    return v1.client.post(
        f"/api/v1/cases/{case_id}/inputs/{role}/example",
        json={"example_id": example_id},
        headers=headers or v1.alice,
    )


def test_disabled_by_default(v1):
    listed = v1.client.get("/api/v1/example-inputs", headers=v1.alice)
    assert listed.status_code == 200 and listed.json() == []
    case_id = v1.new_case()
    r = attach(v1, case_id)
    assert r.status_code == 404 and r.json()["code"] == "example_inputs_disabled"


def test_enabled_without_a_manifest_offers_nothing(v1, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_INPUTS_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    assert v1.client.get("/api/v1/example-inputs", headers=v1.alice).json() == []
    r = attach(v1, v1.new_case())
    assert r.status_code == 404 and r.json()["code"] == "example_not_found"


def test_list_requires_sign_in_and_skips_invalid_entries(v1_on):
    assert v1_on.client.get("/api/v1/example-inputs").status_code == 401
    rows = v1_on.client.get("/api/v1/example-inputs", headers=v1_on.alice).json()
    assert [r["id"] for r in rows] == ["ex-left", "ex-right"]
    assert rows[0] == {
        "id": "ex-left",
        "role": "lesion_mni",
        "label": "Left",
        "description": "Synthetic.",
        "licence": "CC0-1.0",
        "synthetic": True,
    }


def test_attach_stores_the_file_marked_as_a_synthetic_example(v1_on, example_dir):
    v1 = v1_on
    case_id = v1.new_case()
    r = attach(v1, case_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["origin"] == EXAMPLE_ORIGIN and body["role"] == "lesion_mni"
    assert body["size_bytes"] == len((example_dir / "left.nii.gz").read_bytes())
    # Same bytes, readable through the normal download path.
    dl = v1.client.get(
        f"/api/v1/cases/{case_id}/inputs/lesion_mni/download", headers=v1.alice
    )
    assert dl.content == (example_dir / "left.nii.gz").read_bytes()
    detail = v1.client.get(f"/api/v1/cases/{case_id}", headers=v1.alice).json()
    assert [i["origin"] for i in detail["inputs"]] == [EXAMPLE_ORIGIN]
    with v1.Session() as db:
        events = [
            json.loads(e.details)
            for e in db.scalars(select(AuditEvent))
            if e.event_type == "input_example_attached"
        ]
    assert events and events[0]["example_id"] == "ex-left"
    assert not list((v1.root / ".incoming").glob("*"))  # temp directory removed

    # Choosing another example replaces the first (200).
    again = attach(v1, case_id, example_id="ex-right")
    assert again.status_code == 200
    assert again.json()["origin"]["version"] == "ex-right"
    assert again.json()["id"] == body["id"]


def test_attach_errors(v1_on):
    v1 = v1_on
    case_id = v1.new_case()
    r = attach(v1, case_id, example_id="nope")
    assert r.status_code == 404 and r.json()["code"] == "example_not_found"
    r = attach(v1, case_id, example_id="ex-bad")
    assert r.status_code == 404
    r = attach(v1, case_id, role="ct")
    assert r.status_code == 422 and r.json()["code"] == "example_role_mismatch"
    r = v1.client.post(
        f"/api/v1/cases/{case_id}/inputs/lesion_mni/example",
        json={"example_id": "ex-left", "extra": 1},
        headers=v1.alice,
    )
    assert r.status_code == 422


def test_attach_permissions(v1_on):
    v1 = v1_on
    case_id = v1.new_case()
    no_auth = v1.client.post(
        f"/api/v1/cases/{case_id}/inputs/lesion_mni/example",
        json={"example_id": "ex-left"},
    )
    assert no_auth.status_code == 401
    # Another researcher cannot see the case.
    assert attach(v1, case_id, headers=v1.bob).status_code == 404
    # An administrator may, as for uploads.
    assert attach(v1, case_id, headers=v1.admin).status_code == 201


def test_attach_is_refused_once_the_case_has_a_run(v1_on):
    v1 = v1_on
    v1.enable_all()
    case_id = v1.new_case()
    assert attach(v1, case_id).status_code == 201
    assert v1.start(case_id, input_roles=["lesion_mni"]).status_code == 201
    r = attach(v1, case_id, example_id="ex-right")
    assert r.status_code == 409 and r.json()["code"] == "case_has_runs"


def test_attach_checks_the_quota(v1_on):
    v1 = v1_on
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = 5
        db.commit()
    r = attach(v1, v1.new_case())
    assert r.status_code == 413 and r.json()["code"] == "storage_quota_exceeded"
    with v1.Session() as db:
        assert db.scalar(select(InputFile.id)) is None


def test_attach_rejects_a_file_that_is_not_nifti(v1_on, example_dir):
    (example_dir / "left.nii.gz").write_bytes(b"not a nifti")
    r = attach(v1_on, v1_on.new_case())
    assert r.status_code == 422 and r.json()["code"] == "invalid_input_file"


def test_examples_are_never_mixed_with_user_files(v1_on):
    v1 = v1_on
    case_id = v1.new_case()
    assert attach(v1, case_id).status_code == 201
    other = v1.upload(case_id, "ct")
    assert other.status_code == 409 and other.json()["code"] == "example_input_mixed"
    # Replacing the example role with a user file is allowed: nothing else is left to mix.
    assert v1.upload(case_id, "lesion_mni").status_code == 200

    case2 = v1.new_case()
    assert v1.upload(case2, "ct").status_code == 201
    mixed = attach(v1, case2)
    assert mixed.status_code == 409 and mixed.json()["code"] == "example_input_mixed"


def test_the_example_origin_is_reserved(v1_on):
    v1 = v1_on
    case_id = v1.new_case()
    forged = v1.upload(case_id, "lesion_mni", origin=json.dumps(EXAMPLE_ORIGIN))
    assert forged.status_code == 422 and forged.json()["code"] == "invalid_origin"
    assert attach(v1, case_id).status_code == 201
    base = f"/api/v1/cases/{case_id}/inputs/lesion_mni"
    cleared = v1.client.patch(base, json={"origin": None}, headers=v1.alice)
    assert cleared.status_code == 409
    assert cleared.json()["code"] == "example_input_origin_fixed"
    v1.upload(case_id, "lesion_mni")  # a user file replaces the example
    forged = v1.client.patch(base, json={"origin": EXAMPLE_ORIGIN}, headers=v1.alice)
    assert forged.status_code == 422 and forged.json()["code"] == "invalid_origin"
