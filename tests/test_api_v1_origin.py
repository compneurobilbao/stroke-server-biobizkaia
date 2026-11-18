"""Declared origin of supplied inputs (card F1-24, ADR-0018, contract 1.2).

The origin is declared by the uploader and never verified; it is stored in
``input_files.origin``, returned by the case, input and run endpoints, and copied into
the 1.2 run provenance. All data is synthetic bytes.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import AuditEvent, InputFile
from contracts import validate as contract
from orchestrator.engine import TickResult
from tests.test_api_v1 import V1Env, nifti_gz

MANUAL = {"kind": "manual"}
TOOL = {"kind": "external-tool", "tool": "example-tool", "version": "2.1.0+build.3"}


@pytest.fixture()
def v1(client):
    yield V1Env(client)


def put_origin(v1, case_id, role, origin, headers=None):
    return v1.client.patch(
        f"/api/v1/cases/{case_id}/inputs/{role}",
        json={"origin": origin},
        headers=headers or v1.alice,
    )


def listed(v1, case_id):
    rows = v1.client.get(f"/api/v1/cases/{case_id}/inputs", headers=v1.alice).json()
    return {r["role"]: r["origin"] for r in rows}


def audit_details(v1, event_type):
    with v1.Session() as db:
        return [
            json.loads(e.details)
            for e in db.scalars(select(AuditEvent))
            if e.event_type == event_type
        ]


def test_upload_without_origin_has_none(v1):
    case_id = v1.new_case()
    r = v1.upload(case_id, "ct")
    assert r.status_code == 201 and r.json()["origin"] is None
    assert listed(v1, case_id) == {"ct": None}


@pytest.mark.parametrize("origin", [MANUAL, TOOL])
def test_valid_origin_is_stored_and_returned_everywhere(v1, origin):
    case_id = v1.new_case()
    r = v1.upload(case_id, "lesion_mni", origin=json.dumps(origin))
    assert r.status_code == 201, r.text
    assert r.json()["origin"] == origin
    assert listed(v1, case_id) == {"lesion_mni": origin}
    detail = v1.client.get(f"/api/v1/cases/{case_id}", headers=v1.alice).json()
    assert [i["origin"] for i in detail["inputs"]] == [origin]
    with v1.Session() as db:
        assert db.scalar(select(InputFile.origin)) == origin
    events = audit_details(v1, "input_uploaded")
    assert events[0]["origin_kind"] == origin["kind"]
    assert "example-tool" not in json.dumps(events)  # the audit log keeps the kind only


@pytest.mark.parametrize(
    "origin",
    [
        "not json",
        "[]",
        "null",
        "{}",
        '{"kind": "unknown"}',
        '{"kind": "manual", "tool": "x"}',  # extra field
        '{"kind": "external-tool"}',
        '{"kind": "external-tool", "tool": "x"}',
        '{"kind": "external-tool", "tool": "x", "version": ""}',
        '{"kind": "external-tool", "tool": "x", "version": "1.0", "note": "free"}',
        # free text, spaces, e-mail like, slashes, non-ASCII, trailing newline
        '{"kind": "external-tool", "tool": "drawn by Somebody", "version": "1"}',
        '{"kind": "external-tool", "tool": "a@b.org", "version": "1"}',
        '{"kind": "external-tool", "tool": "a/b", "version": "1"}',
        '{"kind": "external-tool", "tool": "herramienta\\u00f1", "version": "1"}',
        '{"kind": "external-tool", "tool": "x", "version": "1\\n"}',
        '{"kind": "external-tool", "tool": "' + "a" * 65 + '", "version": "1"}',
        '{"kind": "platform-run", "run_id": "x", "artifact_id": "y"}',  # deferred
        '{"kind": "manual"}' + " " * 600,  # over the raw length bound
    ],
)
def test_invalid_origin_is_refused_without_echoing_it(v1, origin):
    case_id = v1.new_case()
    r = v1.upload(case_id, "ct", origin=origin)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_origin"
    for fragment in ("Somebody", "a@b.org", "herramienta"):
        assert fragment not in r.text
    assert listed(v1, case_id) == {}  # nothing was stored


def test_name_and_version_at_the_length_limit_are_accepted(v1):
    case_id = v1.new_case()
    origin = {"kind": "external-tool", "tool": "a" * 64, "version": "9" * 64}
    r = v1.upload(case_id, "ct", origin=json.dumps(origin))
    assert r.json()["origin"] == origin


def test_blank_origin_field_means_none(v1):
    case_id = v1.new_case()
    assert v1.upload(case_id, "ct", origin="").json()["origin"] is None


def test_replacing_the_file_replaces_or_clears_the_origin(v1):
    case_id = v1.new_case()
    v1.upload(case_id, "ct", origin=json.dumps(TOOL))
    changed = v1.upload(case_id, "ct", data=nifti_gz(b"v2"), origin=json.dumps(MANUAL))
    assert changed.status_code == 200 and changed.json()["origin"] == MANUAL
    # A new file without a declaration does not inherit the old one.
    cleared = v1.upload(case_id, "ct", data=nifti_gz(b"v3"))
    assert cleared.status_code == 200 and cleared.json()["origin"] is None


def test_refused_upload_keeps_the_previous_origin(v1):
    case_id = v1.new_case()
    v1.upload(case_id, "ct", origin=json.dumps(TOOL))
    bad = v1.upload(case_id, "ct", data=b"not gzip", origin=json.dumps(MANUAL))
    assert bad.status_code == 422
    assert listed(v1, case_id) == {"ct": TOOL}


def test_origin_can_be_changed_and_cleared_without_uploading(v1):
    case_id = v1.new_case()
    v1.upload(case_id, "ct")
    r = put_origin(v1, case_id, "ct", TOOL)
    assert r.status_code == 200 and r.json()["origin"] == TOOL
    assert listed(v1, case_id) == {"ct": TOOL}
    assert put_origin(v1, case_id, "ct", None).json()["origin"] is None
    kinds = [d["origin_kind"] for d in audit_details(v1, "input_origin_changed")]
    assert kinds == ["external-tool", None]


def test_origin_change_validation_and_unknown_input(v1):
    case_id = v1.new_case()
    v1.upload(case_id, "ct")
    for bad in (
        {"kind": "external-tool", "tool": "two words", "version": "1"},
        {"kind": "manual", "extra": 1},
        {"kind": "platform-run"},
    ):
        assert put_origin(v1, case_id, "ct", bad).status_code == 422
    r = v1.client.patch(f"/api/v1/cases/{case_id}/inputs/ct", json={}, headers=v1.alice)
    assert r.status_code == 422
    r = put_origin(v1, case_id, "lesion_mni", MANUAL)
    assert r.json()["code"] == "input_not_found"
    assert listed(v1, case_id) == {"ct": None}


def test_permissions(v1):
    case_id = v1.new_case()  # alice's case
    v1.upload(case_id, "ct")
    # Another researcher cannot see the case, so neither upload nor change.
    r = v1.upload(case_id, "ct", headers=v1.bob, origin=json.dumps(MANUAL))
    assert r.status_code == 404
    assert put_origin(v1, case_id, "ct", MANUAL, headers=v1.bob).status_code == 404
    assert listed(v1, case_id) == {"ct": None}
    # Without credentials nothing is allowed.
    r = v1.client.patch(f"/api/v1/cases/{case_id}/inputs/ct", json={"origin": None})
    assert r.status_code == 401


def test_origin_is_frozen_once_the_case_has_a_run(v1):
    v1.enable_all()
    case_id = v1.native_case()
    assert put_origin(v1, case_id, "ct", MANUAL).status_code == 200
    assert v1.start(case_id).status_code == 201
    r = put_origin(v1, case_id, "ct", TOOL)
    assert r.status_code == 409 and r.json()["code"] == "case_has_runs"
    r = v1.upload(case_id, "ct", origin=json.dumps(TOOL))
    assert r.status_code == 409 and r.json()["code"] == "case_has_runs"
    assert listed(v1, case_id)["ct"] == MANUAL


def test_run_inputs_and_provenance_carry_the_declared_origin(v1):
    v1.enable_all()
    case_id = v1.new_case()
    assert v1.upload(case_id, "ct", origin=json.dumps(MANUAL)).status_code == 201
    r = v1.upload(case_id, "lesion_native", origin=json.dumps(TOOL))
    assert r.status_code == 201
    run = v1.start(case_id).json()
    expected = {"ct": MANUAL, "lesion_native": TOOL}
    assert {i["role"]: i["origin"] for i in run["inputs"]} == expected
    assert v1.drive(run["id"]) is TickResult.FINISHED
    detail = v1.client.get(f"/api/v1/runs/{run['id']}", headers=v1.alice).json()
    assert {i["role"]: i["origin"] for i in detail["inputs"]} == expected
    prov = v1.client.get(
        f"/api/v1/runs/{run['id']}/provenance", headers=v1.alice
    ).json()
    assert prov["contract_version"] == "1.2"
    assert {i["name"]: i.get("origin") for i in prov["inputs"]} == expected
    assert contract.schema_errors(prov, "provenance") == []
    assert contract.check_provenance(prov) == []


def test_inputs_without_declaration_have_no_origin_key_in_provenance(v1):
    _case_id, run_id = v1.finished_run()
    prov = v1.client.get(f"/api/v1/runs/{run_id}/provenance", headers=v1.alice).json()
    assert all("origin" not in i for i in prov["inputs"])
    run = v1.client.get(f"/api/v1/runs/{run_id}", headers=v1.alice).json()
    assert all(i["origin"] is None for i in run["inputs"])
