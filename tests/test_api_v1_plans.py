"""Presets, plan preview, free-mode policy and admin preset endpoints of `/api/v1`
(card F1-21; design docs/design/run-plans.md 2.3, 3.3, 3.5, 3.8).

Two environments behind the same API:

* a synthetic pipeline (``plan-flow`` of ``tests/test_run_plans.py``, with a second
  implementation in one slot) whose runs can be created: permissions, policy, audit,
  preview-equals-create, refusal statuses;
* the real ``stroke-lnm`` pipeline, presets and registry of ``pipelines/`` (steps from
  ``steps/``), for the refusals that need its wiring and for the presets listing.

All files are synthetic bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from app.api.v1 import deps
from app.api.v1.deps import Orchestration, get_orchestration, get_run_queue
from app.api.v1.errors import refusal_status
from app.api.v1.openapi import build_openapi
from app.cli import seed_implementations as seed
from app.core.config import free_mode_allowed, run_plan_policy
from app.main import app
from app.models import (
    AuditEvent,
    PipelineRun,
    PresetSetting,
    RunInput,
    RunSlot,
    SlotImplementationSetting,
)
from orchestrator.catalog import (
    DirectoryPipelineRegistry,
    DirectoryPresetRegistry,
    DirectoryStepCatalog,
    StaticPipelineRegistry,
    StaticPresetRegistry,
    StaticStepCatalog,
    load_preset,
)
from orchestrator.pipeline import load_pipeline
from orchestrator.resolution import recorded_preset_digests
from tests import test_orchestrator as base
from tests import test_run_plans as plans
from tests.test_api import auth_headers
from tests.test_api_v1 import FakeQueue, NoBundles, nifti_gz

ROOT = Path(__file__).resolve().parent.parent
PIPE = "plan-flow"
BASE = f"/api/v1/pipelines/{PIPE}/versions/1.0.0"
FREE_ALLOWED = {
    # (profile, policy) -> roles that may use free mode
    ("on-premise", "presets_only"): set(),
    ("on-premise", "free_for_admins"): {"admin"},
    ("on-premise", "free_for_researchers"): {"admin", "researcher"},
}


# ----------------------------------------------------------------- environments


def _preset(name, input_sets, required, if_available=(), implementations=None):
    doc = {
        "contract_version": "1.2",
        "kind": "Preset",
        "id": name,
        "version": "1.0.0",
        "title": f"Preset {name}",
        "description": f"Synthetic preset {name}.",
        "pipeline": {"id": PIPE, "versions": ">=1.0.0, <2.0.0"},
        "input_sets": list(input_sets),
        "targets": {"required": list(required), "if_available": list(if_available)},
    }
    if implementations:
        doc["implementations"] = implementations
    return yaml.safe_dump(doc, sort_keys=False).encode()


PRESET_FILES = {
    "qc-report": _preset("qc-report", ["native", "mni"], ["report"]),
    "mni-analysis": _preset("mni-analysis", ["mni"], ["disconnection", "report"]),
    "native-norm": _preset("native-norm", ["native"], ["normalisation"]),
    "fixed-disc": _preset(
        "fixed-disc",
        ["mni"],
        ["disconnection"],
        implementations={"disconnection": {"fixed": "disc"}},
    ),
    "default-disc": _preset(
        "default-disc",
        ["mni"],
        ["disconnection"],
        implementations={"disconnection": {"default": "disc-alt"}},
    ),
    "hidden": _preset("hidden", ["mni"], ["report"]),
}
ENABLED_PRESETS = (
    "qc-report",
    "mni-analysis",
    "native-norm",
    "fixed-disc",
    "default-disc",
)


def synthetic_pipeline() -> str:
    """``plan-flow`` with a second implementation of ``disconnection``."""
    text = plans.PLAN_YAML
    disc = f"image: {base.IMAGES['disc']}"
    assert disc in text
    alt = (
        "registry.example.org/test/disc-alt@sha256:"
        + hashlib.sha256(b"alt").hexdigest()
    )
    text = text.replace(
        disc,
        f"{disc}\n        - id: disc-alt\n          step: {{name: disc-alt-step, version: 1.0.0}}\n"
        f"          image: {alt}",
    )
    return text


def synthetic_manifests(report_contract: str) -> list[dict]:
    manifests = dict(base.MANIFESTS)
    manifests["report-step"] = plans._report_manifest(report_contract)
    alt = copy.deepcopy(manifests["disc-step"])
    alt["name"] = "disc-alt-step"
    alt["image"] = {"repository": "registry.example.org/test/disc-alt"}
    manifests["disc-alt-step"] = alt
    return list(manifests.values())


class Env:
    def __init__(self, client, services: Orchestration, monkeypatch):
        self.client = client
        self.monkeypatch = monkeypatch
        self.Session = client.app.state.testing_session_local
        self.services = services
        self.queue = FakeQueue()
        app.dependency_overrides[get_orchestration] = lambda: self.services
        app.dependency_overrides[get_run_queue] = lambda: self.queue
        self.admin, self.admin_id = auth_headers(
            client, "admin@example.org", "secret-pass", role="admin"
        )
        self.alice, self.alice_id = auth_headers(
            client, "alice@example.org", "secret-pass"
        )
        self.bob, self.bob_id = auth_headers(client, "bob@example.org", "secret-pass")
        self.reviewer, _ = auth_headers(
            client, "rev@example.org", "secret-pass", role="reviewer"
        )

    # --- deployment configuration
    def policy(self, policy: str | None, profile: str = "on-premise"):
        self.monkeypatch.setenv("DEPLOYMENT_PROFILE", profile)
        if policy is None:
            self.monkeypatch.delenv("RUN_PLAN_POLICY", raising=False)
        else:
            self.monkeypatch.setenv("RUN_PLAN_POLICY", policy)

    def put(self, path, **body):
        return self.client.put(path, json=body, headers=self.admin)

    def enable_implementations(self, skip=()):
        for slot, impl, default in (
            ("intake", "intake", True),
            ("normalisation", "norm", True),
            ("disconnection", "disc", True),
            ("disconnection", "disc-alt", False),
            ("report", "report", True),
        ):
            if impl in skip:
                continue
            r = self.put(
                f"/api/v1/admin/implementations/{PIPE}/{slot}/{impl}",
                enabled=True,
                is_default=default,
            )
            assert r.status_code == 200, r.text

    def enable_presets(self, names=ENABLED_PRESETS, default="qc-report"):
        for name in names:
            r = self.put(
                f"/api/v1/admin/presets/{PIPE}/{name}",
                enabled=True,
                is_default=name == default,
            )
            assert r.status_code == 200, r.text

    # --- cases
    def case(self, roles, headers=None):
        headers = headers or self.alice
        r = self.client.post("/api/v1/cases", json={}, headers=headers)
        assert r.status_code == 201, r.text
        case_id = r.json()["id"]
        for role in roles:
            r = self.client.put(
                f"/api/v1/cases/{case_id}/inputs/{role}",
                files={
                    "file": ("x.nii.gz", nifti_gz(role.encode()), "application/gzip")
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
        return case_id

    def start(self, case_id, pipeline=PIPE, headers=None, **body):
        return self.client.post(
            f"/api/v1/cases/{case_id}/runs",
            json={"pipeline_id": pipeline, **body},
            headers=headers or self.alice,
        )

    def preview(self, roles, headers=None, pipeline=PIPE, version="1.0.0", **body):
        return self.client.post(
            f"/api/v1/pipelines/{pipeline}/versions/{version}/plan",
            json={"input_roles": list(roles), **body},
            headers=headers or self.alice,
        )

    def counts(self):
        with self.Session() as db:
            return {
                model.__name__: db.scalar(select(func.count()).select_from(model))
                for model in (
                    PipelineRun,
                    RunSlot,
                    RunInput,
                    AuditEvent,
                    PresetSetting,
                    SlotImplementationSetting,
                )
            }

    def audit(self, event_type):
        with self.Session() as db:
            return [
                json.loads(e.details)
                for e in db.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == event_type)
                    .order_by(AuditEvent.timestamp)
                )
            ]


def make_synthetic(client, monkeypatch, *, report_contract="1.2"):
    pipeline = load_pipeline(synthetic_pipeline().encode())
    pipelines = StaticPipelineRegistry([pipeline])
    steps = StaticStepCatalog(synthetic_manifests(report_contract))
    presets = StaticPresetRegistry(
        [load_preset(raw) for raw in PRESET_FILES.values()], pipelines, steps
    )
    attached = {p.id for p in presets.presets(PIPE, "1.0.0")}
    assert attached == set(PRESET_FILES), "every synthetic preset must be valid"
    env = Env(
        client,
        Orchestration(pipelines, steps, NoBundles(), "0.0.0-test", presets),
        monkeypatch,
    )
    env.policy("free_for_researchers")
    return env


@pytest.fixture()
def syn(client, monkeypatch):
    return make_synthetic(client, monkeypatch)


@pytest.fixture()
def syn_ready(syn):
    syn.enable_implementations()
    syn.enable_presets()
    return syn


def slot_rows(slots, key="name", impl="implementation"):
    return {
        s[key]: (s["status"], s["reason"], s["plan_role"], s[impl], s["chosen_by"])
        for s in slots
    }


# --------------------------------------------------------------- the policy itself


@pytest.mark.parametrize(
    ("profile", "env", "expected"),
    [
        ("research-public", None, "presets_only"),
        ("research-public", "", "presets_only"),
        ("research-public", "presets_only", "presets_only"),
        ("mask-only", None, "presets_only"),
        ("mask-only", "presets_only", "presets_only"),
        ("on-premise", None, "free_for_researchers"),
        ("on-premise", "presets_only", "presets_only"),
        ("on-premise", "free_for_admins", "free_for_admins"),
        ("on-premise", "free_for_researchers", "free_for_researchers"),
    ],
)
def test_policy_defaults_per_profile(profile, env, expected):
    values = {} if env is None else {"RUN_PLAN_POLICY": env}
    assert run_plan_policy(profile, values) == expected


@pytest.mark.parametrize("profile", ["research-public", "mask-only"])
@pytest.mark.parametrize("value", ["free_for_admins", "free_for_researchers"])
def test_public_profiles_refuse_any_other_policy(profile, value):
    with pytest.raises(RuntimeError, match="presets_only"):
        run_plan_policy(profile, {"RUN_PLAN_POLICY": value})


def test_unknown_policy_is_refused_on_every_profile():
    for profile in ("research-public", "mask-only", "on-premise"):
        with pytest.raises(RuntimeError, match="RUN_PLAN_POLICY"):
            run_plan_policy(profile, {"RUN_PLAN_POLICY": "everyone"})


def test_free_mode_roles_per_policy():
    table = {
        "presets_only": set(),
        "free_for_admins": {"admin"},
        "free_for_researchers": {"admin", "researcher"},
    }
    for policy, allowed in table.items():
        for role in ("admin", "researcher", "reviewer", "coordinator", "service"):
            assert free_mode_allowed(policy, role) == (role in allowed), (policy, role)


@pytest.mark.parametrize(
    ("profile", "policy", "ok"),
    [
        ("mask-only", "free_for_researchers", False),
        ("research-public", "free_for_admins", False),
        ("research-public", "presets_only", True),
        ("on-premise", "free_for_researchers", True),
    ],
)
def test_application_refuses_to_start_with_a_forbidden_policy(profile, policy, ok):
    env = {
        **os.environ,
        "DEPLOYMENT_PROFILE": profile,
        "RUN_PLAN_POLICY": policy,
        "PYTHONPATH": os.pathsep.join(
            [str(ROOT), str(ROOT / "backend"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    done = subprocess.run(
        [sys.executable, "-c", "import app.api.v1"],
        env=env,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert (done.returncode == 0) == ok, done.stderr[-400:]
    if not ok:
        assert "RUN_PLAN_POLICY must be presets_only" in done.stderr


def test_compose_passes_the_policy_through_without_a_default_of_its_own():
    api = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert (
        api["services"]["api"]["environment"]["RUN_PLAN_POLICY"]
        == "${RUN_PLAN_POLICY:-}"
    )


def test_every_refusal_code_has_its_status():
    expected = {
        403: ["free_mode_not_allowed"],
        404: ["preset_not_found"],
        422: [
            "preset_not_enabled",
            "preset_input_set_mismatch",
            "preset_and_targets",
            "unknown_target",
            "ambiguous_target",
            "target_not_applicable",
            "target_not_guaranteed",
            "target_unreachable",
            "conflicting_targets",
            "empty_request",
            "implementation_fixed_by_preset",
            "implementation_not_enabled",
            "unknown_slot",
            "no_matching_input_set",
            "input_type_mismatch",
            "input_not_in_case",
            "invalid_parameters",
            "implementation_invalid",
        ],
        409: [
            "no_implementation",
            "slot_unsatisfiable",
            "step_contract_too_old",
            "manifest_unavailable",
            "bundle_unavailable",
        ],
    }
    for status, codes in expected.items():
        for code in codes:
            assert refusal_status(code) == status, code


# ---------------------------------------------------------------- GET /config


def test_config_reports_the_policy_and_the_callers_right(syn):
    c = syn.client
    assert c.get("/api/v1/config").status_code == 401
    for policy, profile, who in (
        ("presets_only", "on-premise", {"admin": False, "alice": False}),
        ("free_for_admins", "on-premise", {"admin": True, "alice": False}),
        ("free_for_researchers", "on-premise", {"admin": True, "alice": True}),
        (None, "on-premise", {"admin": True, "alice": True}),
        (None, "research-public", {"admin": False, "alice": False}),
        ("presets_only", "mask-only", {"admin": False, "alice": False}),
    ):
        syn.policy(policy, profile)
        for name, allowed in who.items():
            r = c.get("/api/v1/config", headers=getattr(syn, name))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["deployment_profile"] == profile
            assert body["run_plan_policy"] == run_plan_policy(
                profile, {"RUN_PLAN_POLICY": policy or ""}
            )
            assert body["free_mode_allowed"] is allowed, (policy, profile, name)
    r = c.get("/api/v1/config", headers=syn.reviewer)
    assert r.json()["free_mode_allowed"] is False


# ------------------------------------------------------- GET .../presets listing


def test_presets_listing_shows_enabled_presets_with_plans(syn_ready):
    syn = syn_ready
    c = syn.client
    r = c.get(f"{BASE}/presets", headers=syn.alice)
    assert r.status_code == 200, r.text
    listing = {p["id"]: p for p in r.json()}
    assert set(listing) == set(ENABLED_PRESETS)  # `hidden` is attached but not enabled
    qc = listing["qc-report"]
    assert qc["is_default"] is True and qc["version"] == "1.0.0"
    assert qc["sha256"] == hashlib.sha256(PRESET_FILES["qc-report"]).hexdigest()
    assert qc["input_sets"] == ["native", "mni"]
    assert qc["targets"] == {"required": ["report"], "if_available": []}
    assert qc["available"] is True
    assert [p["input_set"] for p in qc["plans"]] == ["native", "mni"]
    mni_plan = next(p for p in qc["plans"] if p["input_set"] == "mni")
    assert mni_plan["available"] is True and mni_plan["reasons"] == []
    assert slot_rows(mni_plan["slots"]) == {
        "intake": ("selected", None, "upstream", "intake", "deployment_default"),
        "normalisation": ("skipped", "condition_false", None, None, None),
        "disconnection": ("skipped", "not_requested", None, None, None),
        "report": ("selected", None, "target", "report", "deployment_default"),
    }
    assert {s["name"]: s["required_by"] for s in mni_plan["slots"]}[
        "intake"
    ] == "report"
    assert [p["is_default"] for p in listing.values()].count(True) == 1
    assert listing["fixed-disc"]["implementations"] == {
        "disconnection": {"fixed": "disc", "default": None}
    }
    # A preset default shows as chosen by the preset in its plan.
    plan = listing["default-disc"]["plans"][0]
    assert slot_rows(plan["slots"])["disconnection"][3:] == ("disc-alt", "preset")
    # Listing is the same for administrators.
    assert c.get(f"{BASE}/presets", headers=syn.admin).json() == r.json()


def test_presets_listing_marks_what_the_deployment_cannot_run(syn_ready):
    syn = syn_ready
    c = syn.client
    for impl in ("norm",):
        r = syn.put(
            f"/api/v1/admin/implementations/{PIPE}/normalisation/{impl}",
            enabled=False,
            is_default=False,
        )
        assert r.status_code == 200
    listing = {p["id"]: p for p in c.get(f"{BASE}/presets", headers=syn.alice).json()}
    norm = listing["native-norm"]
    assert norm["available"] is False
    (plan,) = norm["plans"]
    assert plan["available"] is False
    (reason,) = plan["reasons"]
    assert (reason["code"], reason["slot"], reason["target"]) == (
        "no_implementation",
        "normalisation",
        "normalisation",
    )
    assert listing["qc-report"]["available"] is True
    # Nothing enabled: nothing listed. A disabled preset is hidden again.
    syn.put(
        f"/api/v1/admin/presets/{PIPE}/native-norm", enabled=False, is_default=False
    )
    ids = {p["id"] for p in c.get(f"{BASE}/presets", headers=syn.alice).json()}
    assert "native-norm" not in ids and "qc-report" in ids


def test_presets_listing_older_report_makes_presets_unavailable(client, monkeypatch):
    syn = make_synthetic(client, monkeypatch, report_contract="1.0")
    syn.enable_implementations()
    syn.enable_presets()
    listing = {
        p["id"]: p for p in client.get(f"{BASE}/presets", headers=syn.alice).json()
    }
    assert listing["qc-report"]["available"] is False
    for plan in listing["qc-report"]["plans"]:
        assert [r["code"] for r in plan["reasons"]] == ["step_contract_too_old"]
        assert plan["reasons"][0]["slot"] == "report"
    # A preset that plans no step reading the run record is unaffected.
    assert listing["native-norm"]["available"] is True


def test_presets_listing_permissions_and_errors(syn_ready):
    syn = syn_ready
    c = syn.client
    assert c.get(f"{BASE}/presets").status_code == 401
    assert c.get(f"{BASE}/presets", headers=syn.bob).status_code == 200
    r = c.get(f"/api/v1/pipelines/{PIPE}/versions/9.9.9/presets", headers=syn.alice)
    assert (r.status_code, r.json()["code"]) == (404, "pipeline_not_found")
    r = c.get("/api/v1/pipelines/nope/versions/1.0.0/presets", headers=syn.alice)
    assert (r.status_code, r.json()["code"]) == (404, "pipeline_not_found")
    # No registry of presets: an empty list, not an error.
    syn.services = Orchestration(
        syn.services.pipelines, syn.services.steps, NoBundles(), "0.0.0-test"
    )
    assert c.get(f"{BASE}/presets", headers=syn.alice).json() == []


# ----------------------------------------------------------- POST .../plan preview


def test_preview_returns_the_plan_the_run_gets(syn_ready):
    syn = syn_ready
    cases = [
        # (roles, body)
        (["ct", "lesion_native"], {}),
        (["lesion_mni"], {}),
        (["lesion_mni"], {"implementations": {"disconnection": "disc-alt"}}),
        (["ct", "lesion_native"], {"preset": {"id": "qc-report"}}),
        (["lesion_mni"], {"preset": {"id": "mni-analysis", "version": "1.0.0"}}),
        (["lesion_mni"], {"preset": {"id": "default-disc"}}),
        (["lesion_mni"], {"preset": {"id": "fixed-disc"}}),
        (
            ["ct", "lesion_native"],
            {
                "targets": {
                    "required": ["tract_disconnection"],
                    "if_available": ["report"],
                }
            },
        ),
        (
            ["lesion_mni"],
            {"targets": {"required": ["disconnection.tract_disconnection"]}},
        ),
        (["lesion_mni"], {"targets": {"required": ["intake"]}}),
    ]
    for roles, body in cases:
        before = syn.counts()
        preview = syn.preview(roles, **body)
        assert preview.status_code == 200, preview.text
        assert syn.counts() == before, "preview wrote something"
        plan = preview.json()
        assert plan["feasible"] is True and plan["reasons"] == []
        case_id = syn.case(roles)
        created = syn.start(case_id, **body)
        assert created.status_code == 201, created.text
        run = created.json()
        assert plan["input_set"] == run["input_set"]
        assert plan["mode"] == run["plan"]["mode"]
        assert plan["preset"] == run["plan"]["preset"]
        assert plan["targets"] == run["plan"]["targets"]
        got = {
            s["slot"]: (
                s["status"],
                s["reason"],
                s["plan_role"],
                s["implementation_id"],
                s["chosen_by"],
            )
            for s in run["slots"]
        }
        assert slot_rows(plan["slots"]) == got, body
        assert [s["name"] for s in plan["slots"]] == [s["slot"] for s in run["slots"]]


def test_preview_required_by_follows_the_wiring(syn_ready):
    syn = syn_ready
    plan = syn.preview(
        ["ct", "lesion_native"], targets={"required": ["tract_disconnection"]}
    ).json()
    rows = {s["name"]: (s["plan_role"], s["required_by"]) for s in plan["slots"]}
    assert rows["disconnection"] == ("target", None)
    assert rows["normalisation"] == ("upstream", "disconnection")
    assert rows["intake"] == (None, None)
    assert plan["targets"] == {
        "required": ["disconnection.tract_disconnection"],
        "if_available": [],
    }


def test_preview_refusals_are_a_plan_with_every_reason(syn_ready):
    syn = syn_ready
    r = syn.preview(
        ["lesion_mni"], targets={"required": ["no-such-thing", "normalisation"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is False and body["mode"] == "free"
    assert [x["code"] for x in body["reasons"]] == [
        "unknown_target",
        "target_not_applicable",
    ]
    assert all(x["message"] for x in body["reasons"])

    r = syn.preview(["lesion_mni"], preset={"id": "missing"}).json()
    assert (r["feasible"], r["mode"], r["slots"]) == (False, "preset", [])
    assert [x["code"] for x in r["reasons"]] == ["preset_not_found"]

    r = syn.preview(["lesion_mni"], preset={"id": "hidden"}).json()
    assert [x["code"] for x in r["reasons"]] == ["preset_not_enabled"]
    assert r["preset"] == {
        "id": "hidden",
        "version": "1.0.0",
        "sha256": hashlib.sha256(PRESET_FILES["hidden"]).hexdigest(),
    }

    r = syn.preview(["ct", "lesion_native"], preset={"id": "mni-analysis"}).json()
    assert [x["code"] for x in r["reasons"]] == ["preset_input_set_mismatch"]

    r = syn.preview(
        ["lesion_mni"], preset={"id": "qc-report"}, targets={"required": ["report"]}
    ).json()
    assert [x["code"] for x in r["reasons"]] == ["preset_and_targets"]

    r = syn.preview(
        ["lesion_mni"],
        preset={"id": "fixed-disc"},
        implementations={"disconnection": "disc-alt"},
    ).json()
    assert [x["code"] for x in r["reasons"]] == ["implementation_fixed_by_preset"]
    assert r["reasons"][0]["slot"] == "disconnection"

    r = syn.preview(["lesion_mni"], implementations={"report": "no-such-impl"}).json()
    assert [x["code"] for x in r["reasons"]] == ["implementation_not_enabled"]

    r = syn.preview(
        ["lesion_mni"], targets={"required": ["report"], "if_available": ["report"]}
    ).json()
    assert [x["code"] for x in r["reasons"]] == ["conflicting_targets"]
    r = syn.preview(["lesion_mni"], targets={"if_available": ["normalisation"]}).json()
    assert [x["code"] for x in r["reasons"]] == ["empty_request"]
    r = syn.preview(["lesion_mni"], targets={}).json()
    assert [x["code"] for x in r["reasons"]] == ["empty_request"]


def test_preview_no_implementation_names_the_slot_and_what_needs_it(syn_ready):
    syn = syn_ready
    for impl in ("disc", "disc-alt"):
        syn.put(
            f"/api/v1/admin/implementations/{PIPE}/disconnection/{impl}",
            enabled=False,
            is_default=False,
        )
    body = syn.preview(["lesion_mni"], preset={"id": "mni-analysis"}).json()
    assert body["feasible"] is False
    reasons = {(r["code"], r["slot"], r["target"]) for r in body["reasons"]}
    assert ("no_implementation", "disconnection", "disconnection") in reasons
    full = syn.preview(["lesion_mni"]).json()
    assert [r["code"] for r in full["reasons"]][0] == "no_implementation"
    assert full["reasons"][0]["slot"] == "disconnection"
    assert full["mode"] == "full"


def test_preview_step_contract_too_old(client, monkeypatch):
    syn = make_synthetic(client, monkeypatch, report_contract="1.0")
    syn.enable_implementations()
    syn.enable_presets()
    r = syn.preview(["lesion_mni"], preset={"id": "qc-report"}).json()
    assert r["feasible"] is False
    assert [(x["code"], x["slot"]) for x in r["reasons"]] == [
        ("step_contract_too_old", "report")
    ]
    assert slot_rows(r["slots"])["report"][0] == "selected"  # the plan itself is shown
    # The full plan is never affected.
    assert syn.preview(["lesion_mni"]).json()["feasible"] is True


def test_preview_malformed_requests_are_422(syn_ready):
    syn = syn_ready
    for roles, body, code in (
        (["lesion_mni"], {"implementations": {"nope": "x"}}, "unknown_slot"),
        (["ct"], {}, "no_matching_input_set"),
        (["ct", "lesion_native", "lesion_mni"], {}, "no_matching_input_set"),
        (["not_a_role"], {}, "input_type_mismatch"),
    ):
        r = syn.preview(roles, **body)
        assert (r.status_code, r.json()["code"]) == (422, code), (roles, body)
    for body in (
        {"input_roles": []},
        {"input_roles": ["lesion_mni"], "extra": 1},
        {"input_roles": ["lesion_mni"], "preset": {"id": "Not Kebab"}},
        {"input_roles": ["lesion_mni"], "targets": {"required": [""]}},
        {"input_roles": ["lesion_mni"], "targets": {"required": ["x"] * 65}},
    ):
        r = syn.client.post(f"{BASE}/plan", json=body, headers=syn.alice)
        assert r.status_code == 422, body
    r = syn.client.post(
        f"/api/v1/pipelines/{PIPE}/versions/7.0.0/plan",
        json={"input_roles": ["lesion_mni"]},
        headers=syn.alice,
    )
    assert (r.status_code, r.json()["code"]) == (404, "pipeline_not_found")


def test_preview_policy_and_permissions(syn_ready):
    syn = syn_ready
    c = syn.client
    free = {"targets": {"required": ["report"]}}
    assert (
        c.post(f"{BASE}/plan", json={"input_roles": ["lesion_mni"]}).status_code == 401
    )
    for policy, profile in (
        ("presets_only", "on-premise"),
        ("free_for_admins", "on-premise"),
        ("free_for_researchers", "on-premise"),
        (None, "on-premise"),
        (None, "research-public"),
        ("presets_only", "mask-only"),
    ):
        syn.policy(policy, profile)
        effective = run_plan_policy(profile, {"RUN_PLAN_POLICY": policy or ""})
        for who in ("admin", "alice", "bob", "reviewer"):
            headers = getattr(syn, who)
            role = "admin" if who == "admin" else "researcher"
            # Presets and the full plan: everyone allowed to create runs.
            for body in ({}, {"preset": {"id": "qc-report"}}):
                r = syn.preview(["lesion_mni"], headers=headers, **body)
                if who == "reviewer":
                    assert (r.status_code, r.json()["code"]) == (
                        403,
                        "permission_denied",
                    )
                else:
                    assert r.status_code == 200, (policy, who, body, r.text)
            r = syn.preview(["lesion_mni"], headers=headers, **free)
            if who == "reviewer":
                assert (r.status_code, r.json()["code"]) == (403, "permission_denied")
            elif free_mode_allowed(effective, role):
                assert r.status_code == 200, (policy, profile, who, r.text)
                assert r.json()["mode"] == "free"
            else:
                assert (r.status_code, r.json()["code"]) == (
                    403,
                    "free_mode_not_allowed",
                ), (
                    policy,
                    profile,
                    who,
                )
    # The policy is checked before anything else about the request is looked at.
    syn.policy("presets_only")
    r = c.post(
        "/api/v1/pipelines/nope/versions/1.0.0/plan",
        json={"input_roles": ["x"], "targets": {"required": ["report"]}},
        headers=syn.alice,
    )
    assert (r.status_code, r.json()["code"]) == (403, "free_mode_not_allowed")


# -------------------------------------------------------------- POST .../runs


def test_create_preset_run_records_the_plan(syn_ready):
    syn = syn_ready
    case_id = syn.case(["lesion_mni"])
    r = syn.start(case_id, preset={"id": "mni-analysis"})
    assert r.status_code == 201, r.text
    run = r.json()
    sha = hashlib.sha256(PRESET_FILES["mni-analysis"]).hexdigest()
    assert run["plan"] == {
        "mode": "preset",
        "preset": {"id": "mni-analysis", "version": "1.0.0", "sha256": sha},
        "targets": {
            "required": ["disconnection.tract_disconnection", "report.report_json"],
            "if_available": [],
        },
    }
    assert {s["slot"]: s["plan_role"] for s in run["slots"]} == {
        "intake": "upstream",
        "normalisation": None,
        "disconnection": "target",
        "report": "target",
    }
    again = syn.client.get(f"/api/v1/runs/{run['id']}", headers=syn.alice).json()
    assert again["plan"] == run["plan"]
    listed = syn.client.get(f"/api/v1/cases/{case_id}/runs", headers=syn.alice).json()
    assert listed[0]["id"] == run["id"]
    with syn.Session() as db:
        row = db.get(PipelineRun, uuid.UUID(run["id"]))
        assert (
            row.plan_mode,
            row.preset_id,
            row.preset_version,
            row.preset_sha256,
        ) == (
            "preset",
            "mni-analysis",
            "1.0.0",
            sha,
        )
    (audit,) = syn.audit("run_created")
    assert audit["plan_mode"] == "preset"
    assert (audit["preset"], audit["preset_version"]) == ("mni-analysis", "1.0.0")
    assert audit["targets"] == {
        "required": ["disconnection.tract_disconnection", "report.report_json"]
    }
    assert audit["pipeline_id"] == PIPE


def test_create_full_and_free_runs_record_their_mode(syn_ready):
    syn = syn_ready
    full = syn.start(syn.case(["lesion_mni"]))
    assert full.status_code == 201
    assert full.json()["plan"] == {
        "mode": "full",
        "preset": None,
        "targets": {"required": [], "if_available": []},
    }
    free = syn.start(
        syn.case(["ct", "lesion_native"]),
        targets={"required": ["tract_disconnection"], "if_available": ["report"]},
    )
    assert free.status_code == 201, free.text
    plan = free.json()["plan"]
    assert plan["mode"] == "free" and plan["preset"] is None
    assert plan["targets"]["required"] == ["disconnection.tract_disconnection"]
    roles = {
        s["slot"]: (s["status"], s["reason"], s["plan_role"])
        for s in free.json()["slots"]
    }
    assert roles["normalisation"] == ("selected", None, "upstream")
    assert roles["intake"][1] == "not_requested" or roles["intake"][2] == "if_available"
    first, second = syn.audit("run_created")
    assert (first["plan_mode"], first["preset"], first["targets"]) == ("full", None, {})
    assert (second["plan_mode"], second["preset"]) == ("free", None)
    assert second["targets"]["required"] == ["disconnection.tract_disconnection"]


def test_create_free_mode_policy_matrix(syn_ready):
    syn = syn_ready
    body = {"targets": {"required": ["report"]}}
    for policy, profile in (
        ("presets_only", "on-premise"),
        ("free_for_admins", "on-premise"),
        ("free_for_researchers", "on-premise"),
        (None, "research-public"),
        (None, "mask-only"),
    ):
        syn.policy(policy, profile)
        effective = run_plan_policy(profile, {"RUN_PLAN_POLICY": policy or ""})
        for who, role in (("admin", "admin"), ("alice", "researcher")):
            headers = getattr(syn, who)
            case_id = syn.case(["lesion_mni"], headers=headers)
            before = syn.counts()
            r = syn.start(case_id, headers=headers, **body)
            if free_mode_allowed(effective, role):
                assert r.status_code == 201, (policy, profile, who, r.text)
            else:
                assert (r.status_code, r.json()["code"]) == (
                    403,
                    "free_mode_not_allowed",
                )
                assert syn.counts() == before, "a refused request wrote something"
            # A preset or the full plan stays open to both roles.
            other = syn.case(["lesion_mni"], headers=headers)
            assert (
                syn.start(
                    other, headers=headers, preset={"id": "qc-report"}
                ).status_code
                == 201
            )
    # Other users' cases stay invisible (404), whatever the policy.
    case_id = syn.case(["lesion_mni"])
    r = syn.start(case_id, headers=syn.bob, **body)
    assert (r.status_code, r.json()["code"]) == (404, "case_not_found")
    syn.policy("presets_only")
    r = syn.start(case_id, headers=syn.bob, **body)
    assert (r.status_code, r.json()["code"]) == (404, "case_not_found")
    r = syn.start(case_id, headers=syn.reviewer, **body)
    assert r.status_code == 403
    assert r.json()["code"] == "permission_denied"


def test_create_refusals_map_to_statuses_and_list_every_reason(syn_ready):
    syn = syn_ready
    mni = ["lesion_mni"]
    native = ["ct", "lesion_native"]
    cases = [
        (mni, {"preset": {"id": "missing"}}, 404, "preset_not_found"),
        (
            mni,
            {"preset": {"id": "qc-report", "version": "9.0.0"}},
            404,
            "preset_not_found",
        ),
        (mni, {"preset": {"id": "hidden"}}, 422, "preset_not_enabled"),
        (native, {"preset": {"id": "mni-analysis"}}, 422, "preset_input_set_mismatch"),
        (
            mni,
            {"preset": {"id": "qc-report"}, "targets": {"required": ["report"]}},
            422,
            "preset_and_targets",
        ),
        (mni, {"targets": {"required": ["no-such-thing"]}}, 422, "unknown_target"),
        (
            mni,
            {"targets": {"required": ["normalisation"]}},
            422,
            "target_not_applicable",
        ),
        (
            mni,
            {"targets": {"required": ["report"], "if_available": ["report"]}},
            422,
            "conflicting_targets",
        ),
        (mni, {"targets": {}}, 422, "empty_request"),
        (
            mni,
            {
                "preset": {"id": "fixed-disc"},
                "implementations": {"disconnection": "disc-alt"},
            },
            422,
            "implementation_fixed_by_preset",
        ),
        (
            mni,
            {"implementations": {"report": "no-such-impl"}},
            422,
            "implementation_not_enabled",
        ),
        (mni, {"implementations": {"nope": "x"}}, 422, "unknown_slot"),
    ]
    for roles, body, status, code in cases:
        case_id = syn.case(roles)
        before = syn.counts()
        r = syn.start(case_id, **body)
        assert (r.status_code, r.json()["code"]) == (status, code), (body, r.text)
        assert syn.counts() == before, f"{code} wrote something"
        # Preview shows the same reasons (when it can show a plan at all).
        preview = syn.preview(roles, **body)
        if code == "unknown_slot":
            assert (preview.status_code, preview.json()["code"]) == (422, code)
        else:
            assert preview.status_code == 200, preview.text
            assert preview.json()["feasible"] is False
            assert preview.json()["reasons"] == r.json()["reasons"], code
    # Every reason of a refusal is in the body, the first gives code and slot.
    case_id = syn.case(mni)
    r = syn.start(case_id, targets={"required": ["no-such-thing", "normalisation"]})
    body = r.json()
    assert [x["code"] for x in body["reasons"]] == [
        "unknown_target",
        "target_not_applicable",
    ]
    assert body["code"] == body["reasons"][0]["code"]
    # Errors that are not about a plan carry no `reasons`.
    r = syn.start(case_id, implementations={"nope": "x"})
    assert "reasons" not in r.json()


def test_create_no_implementation_is_409_with_required_by(syn_ready):
    syn = syn_ready
    for impl in ("disc", "disc-alt"):
        syn.put(
            f"/api/v1/admin/implementations/{PIPE}/disconnection/{impl}",
            enabled=False,
            is_default=False,
        )
    case_id = syn.case(["lesion_mni"])
    r = syn.start(case_id, preset={"id": "mni-analysis"})
    assert (r.status_code, r.json()["code"]) == (409, "no_implementation")
    assert r.json()["slot"] == "disconnection"
    (reason,) = r.json()["reasons"]
    assert (reason["slot"], reason["target"]) == ("disconnection", "disconnection")
    # The full plan: the code and slot of run creation before plans existed.
    r = syn.start(case_id)
    assert (r.status_code, r.json()["code"], r.json()["slot"]) == (
        409,
        "no_implementation",
        "disconnection",
    )


def test_create_step_contract_too_old_is_409_and_writes_nothing(client, monkeypatch):
    syn = make_synthetic(client, monkeypatch, report_contract="1.0")
    syn.enable_implementations()
    syn.enable_presets()
    case_id = syn.case(["lesion_mni"])
    before = syn.counts()
    r = syn.start(case_id, preset={"id": "qc-report"})
    assert (r.status_code, r.json()["code"], r.json()["slot"]) == (
        409,
        "step_contract_too_old",
        "report",
    )
    assert syn.counts() == before
    free = syn.start(case_id, targets={"required": ["report"]})
    assert (free.status_code, free.json()["code"]) == (409, "step_contract_too_old")
    # The full plan still runs, and so does a preset that plans no such step.
    assert syn.start(case_id).status_code == 201


def test_create_requires_the_deployment_presets_to_exist(syn):
    # Nothing enabled in this deployment yet: presets are refused, the full plan too.
    syn.enable_implementations()
    case_id = syn.case(["lesion_mni"])
    r = syn.start(case_id, preset={"id": "qc-report"})
    assert (r.status_code, r.json()["code"]) == (422, "preset_not_enabled")
    assert syn.start(case_id).status_code == 201


# ------------------------------------------------------------- admin endpoints


def test_admin_presets_list_enable_disable_default_with_audit(syn):
    c = syn.client
    r = c.get("/api/v1/admin/presets", headers=syn.admin)
    assert r.status_code == 200
    rows = {p["preset_id"]: p for p in r.json()}
    assert set(rows) == set(PRESET_FILES)
    assert all(
        p["enabled"] is False and p["is_default"] is False for p in rows.values()
    )
    assert rows["qc-report"]["pipeline_id"] == PIPE
    assert rows["qc-report"]["versions"] == ["1.0.0"]
    assert rows["qc-report"]["pipeline_versions"] == ["1.0.0"]
    assert rows["qc-report"]["title"] == "Preset qc-report"
    assert rows["qc-report"]["updated_at"] is None
    assert (
        c.get("/api/v1/admin/presets?pipeline_id=nope", headers=syn.admin).json() == []
    )
    assert (
        len(
            c.get(f"/api/v1/admin/presets?pipeline_id={PIPE}", headers=syn.admin).json()
        )
        == 6
    )

    url = f"/api/v1/admin/presets/{PIPE}"
    r = c.put(
        f"{url}/qc-report",
        json={"enabled": True, "is_default": True},
        headers=syn.admin,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["enabled"], body["is_default"]) == (True, True)
    assert body["updated_by_user_id"] == str(syn.admin_id)
    assert body["updated_at"] is not None
    # Another default replaces it.
    r = c.put(
        f"{url}/mni-analysis",
        json={"enabled": True, "is_default": True},
        headers=syn.admin,
    )
    assert r.status_code == 200
    rows = {
        p["preset_id"]: p
        for p in c.get("/api/v1/admin/presets", headers=syn.admin).json()
    }
    assert (rows["mni-analysis"]["is_default"], rows["qc-report"]["is_default"]) == (
        True,
        False,
    )
    assert rows["qc-report"]["enabled"] is True
    # Enable without default, disable the default.
    c.put(f"{url}/native-norm", json={"enabled": True}, headers=syn.admin)
    r = c.put(f"{url}/mni-analysis", json={"enabled": False}, headers=syn.admin)
    assert (r.json()["enabled"], r.json()["is_default"]) == (False, False)
    rows = {
        p["preset_id"]: p
        for p in c.get("/api/v1/admin/presets", headers=syn.admin).json()
    }
    assert [k for k, v in rows.items() if v["is_default"]] == []
    # The listing for users follows.
    syn.enable_implementations()
    ids = {p["id"] for p in c.get(f"{BASE}/presets", headers=syn.alice).json()}
    assert ids == {"qc-report", "native-norm"}
    with syn.Session() as db:
        assert db.scalar(select(func.count()).select_from(PresetSetting)) == 3
    events = syn.audit("preset_setting_changed")
    assert events[0] == {
        "pipeline_id": PIPE,
        "preset_id": "qc-report",
        "enabled": True,
        "is_default": True,
    }
    assert len(events) == 4
    assert events[-1]["enabled"] is False and events[-1]["preset_id"] == "mni-analysis"


def test_admin_presets_errors_and_permissions(syn):
    c = syn.client
    url = f"/api/v1/admin/presets/{PIPE}/qc-report"
    assert c.get("/api/v1/admin/presets").status_code == 401
    assert c.put(url, json={"enabled": True}).status_code == 401
    for headers in (syn.alice, syn.reviewer):
        assert c.get("/api/v1/admin/presets", headers=headers).status_code == 403
        r = c.put(url, json={"enabled": True}, headers=headers)
        assert (r.status_code, r.json()["code"]) == (403, "permission_denied")
    with syn.Session() as db:
        assert db.scalar(select(func.count()).select_from(PresetSetting)) == 0
    r = c.put(
        f"/api/v1/admin/presets/{PIPE}/nope", json={"enabled": True}, headers=syn.admin
    )
    assert (r.status_code, r.json()["code"]) == (404, "preset_not_found")
    r = c.put(
        "/api/v1/admin/presets/nope/qc-report",
        json={"enabled": True},
        headers=syn.admin,
    )
    assert (r.status_code, r.json()["code"]) == (404, "preset_not_found")
    for body in ({"enabled": False, "is_default": True}, {}, {"enabled": True, "x": 1}):
        assert c.put(url, json=body, headers=syn.admin).status_code == 422
    assert syn.audit("preset_setting_changed") == []


def test_disabling_a_preset_hides_it_but_keeps_runs_readable(syn_ready):
    syn = syn_ready
    case_id = syn.case(["lesion_mni"])
    run = syn.start(case_id, preset={"id": "qc-report"}).json()
    syn.put(f"/api/v1/admin/presets/{PIPE}/qc-report", enabled=False, is_default=False)
    got = syn.client.get(f"/api/v1/runs/{run['id']}", headers=syn.alice).json()
    assert got["plan"]["preset"]["id"] == "qc-report"
    r = syn.start(syn.case(["lesion_mni"]), preset={"id": "qc-report"})
    assert (r.status_code, r.json()["code"]) == (422, "preset_not_enabled")


# ------------------------------------------------------------ the real pipeline


@pytest.fixture()
def real(client, monkeypatch):
    pipelines = DirectoryPipelineRegistry(ROOT / "pipelines")
    assert [p.id for p in pipelines.all()] == ["stroke-lnm"]
    directory = DirectoryStepCatalog(ROOT / "steps")
    manifests = [dict(m) for m in directory._items.values()]

    def build(report_contract: str):
        patched = []
        for m in manifests:
            m = copy.deepcopy(m)
            if m["name"].startswith("research-report"):
                m["contract_version"] = report_contract
            patched.append(m)
        steps = StaticStepCatalog(patched)
        presets = DirectoryPresetRegistry(ROOT / "pipelines", pipelines, steps)
        return Orchestration(pipelines, steps, NoBundles(), "0.0.0-test", presets)

    env = Env(client, build("1.2"), monkeypatch)
    env.build = build
    env.policy("free_for_researchers")
    _path, registry = seed.find_registry_files(ROOT / "pipelines")[0]
    preset_ids, _failed = seed.preset_ids_by_pipeline(ROOT / "pipelines")
    with env.Session() as db:
        seed.seed_registry(
            db, registry, "deployment", preset_ids=preset_ids["stroke-lnm"]
        )
        db.commit()
    return env


REAL = "stroke-lnm"
REAL_BASE = f"/api/v1/pipelines/{REAL}/versions/0.4.0"
CT = ["ct", "lesion_native"]
MNI = ["lesion_mni"]


def test_real_pipeline_lists_the_four_presets(real):
    r = real.client.get(f"{REAL_BASE}/presets", headers=real.alice)
    assert r.status_code == 200, r.text
    listing = {p["id"]: p for p in r.json()}
    assert set(listing) == {
        "full",
        "mni-mask-analysis",
        "ct-normalisation",
        "lesion-qc-report",
    }
    assert [p["id"] for p in listing.values() if p["is_default"]] == ["full"]
    assert all(p["version"] == "1.2.0" for p in listing.values())
    ct = listing["ct-normalisation"]
    assert ct["input_sets"] == ["ct-and-native-mask"]
    (plan,) = ct["plans"]
    assert plan["available"] is True and ct["available"] is True
    rows = {s["name"]: s["plan_role"] for s in plan["slots"]}
    assert rows["lesion-intake"] == "target" and rows["normalisation"] == "target"
    states = {s["name"]: (s["status"], s["reason"]) for s in plan["slots"]}
    assert states["report"] == ("skipped", "not_requested")
    mni = listing["mni-mask-analysis"]
    assert [p["input_set"] for p in mni["plans"]] == ["mni-mask"]
    assert mni["plans"][0]["available"] is True
    assert {s["name"]: s["status"] for s in mni["plans"][0]["slots"]}[
        "functional-disconnection"
    ] == "selected"
    assert [p["input_set"] for p in listing["full"]["plans"]] == [
        "ct-and-native-mask",
        "mni-mask",
    ]


def test_real_pipeline_report_contract_gates_presets_that_plan_the_report(real):
    real.services = real.build("1.0")
    listing = {
        p["id"]: p
        for p in real.client.get(f"{REAL_BASE}/presets", headers=real.alice).json()
    }
    for name in ("full", "mni-mask-analysis", "lesion-qc-report"):
        assert listing[name]["available"] is False, name
        for plan in listing[name]["plans"]:
            assert [(r["code"], r["slot"]) for r in plan["reasons"]] == [
                ("step_contract_too_old", "report")
            ]
    assert listing["ct-normalisation"]["available"] is True


def test_real_pipeline_unmounted_normalisation(real):
    r = real.put(
        f"/api/v1/admin/implementations/{REAL}/normalisation/ct-lesion-to-mni152",
        enabled=False,
        is_default=False,
    )
    assert r.status_code == 200
    listing = {
        p["id"]: p
        for p in real.client.get(f"{REAL_BASE}/presets", headers=real.alice).json()
    }
    ct = listing["ct-normalisation"]
    assert ct["available"] is False
    assert [(x["code"], x["slot"]) for x in ct["plans"][0]["reasons"]] == [
        ("no_implementation", "normalisation")
    ]
    full_plans = {p["input_set"]: p for p in listing["full"]["plans"]}
    assert full_plans["mni-mask"]["available"] is True
    assert full_plans["ct-and-native-mask"]["available"] is False


REAL_REFUSALS = [
    # (roles, body, code, status)
    (MNI, {"targets": {"required": ["no-such-slot"]}}, "unknown_target", 422),
    (CT, {"targets": {"required": ["qc_figure"]}}, "ambiguous_target", 422),
    (MNI, {"targets": {"required": ["normalisation"]}}, "target_not_applicable", 422),
    (
        CT,
        {"targets": {"required": ["normalisation.ct_mni"]}},
        "target_not_guaranteed",
        422,
    ),
    (
        MNI,
        {"targets": {"required": ["report"], "if_available": ["report"]}},
        "conflicting_targets",
        422,
    ),
    (MNI, {"targets": {"if_available": ["normalisation"]}}, "empty_request", 422),
    (CT, {"preset": {"id": "mni-mask-analysis"}}, "preset_input_set_mismatch", 422),
    (MNI, {"preset": {"id": "no-such-preset"}}, "preset_not_found", 404),
    (
        MNI,
        {"preset": {"id": "full"}, "targets": {"required": ["report"]}},
        "preset_and_targets",
        422,
    ),
]


@pytest.mark.parametrize(("roles", "body", "code", "status"), REAL_REFUSALS)
def test_real_pipeline_refusals_preview_and_create_agree(
    real, roles, body, code, status
):
    case_id = real.case(roles)
    before = real.counts()
    created = real.start(case_id, pipeline=REAL, pipeline_version="0.4.0", **body)
    assert (created.status_code, created.json()["code"]) == (status, code), created.text
    assert real.counts() == before
    preview = real.preview(roles, pipeline=REAL, version="0.4.0", **body)
    assert preview.status_code == 200, preview.text
    shown = preview.json()
    assert shown["feasible"] is False
    assert [r["code"] for r in shown["reasons"]][0] == code
    assert shown["reasons"] == created.json()["reasons"]
    assert real.counts() == before


def test_real_pipeline_free_mode_adds_what_the_target_needs(real):
    plan = real.preview(
        CT,
        pipeline=REAL,
        version="0.4.0",
        targets={
            "required": ["tract_disconnection"],
            "if_available": ["cognet-profile"],
        },
    ).json()
    assert plan["feasible"] is True, plan
    rows = {s["name"]: (s["plan_role"], s["status"]) for s in plan["slots"]}
    assert rows["tract-disconnection"] == ("target", "selected")
    assert rows["normalisation"][0] == "upstream"
    assert rows["report"][1] == "skipped"
    needed = {s["name"]: s["required_by"] for s in plan["slots"]}
    assert needed["normalisation"] is not None
    # Under presets_only the same request is refused with 403 and writes nothing.
    real.policy("presets_only")
    before = real.counts()
    r = real.preview(
        CT,
        pipeline=REAL,
        version="0.4.0",
        targets={"required": ["tract_disconnection"]},
    )
    assert (r.status_code, r.json()["code"]) == (403, "free_mode_not_allowed")
    assert real.counts() == before


def test_p8_recorded_digest_hides_a_changed_preset(real, tmp_path):
    case_id = real.case(CT)
    r = real.start(
        case_id,
        pipeline=REAL,
        pipeline_version="0.4.0",
        preset={"id": "ct-normalisation"},
    )
    # Run creation needs the registered bundles of the selected steps, which this
    # test does not have; what matters is whether a digest got recorded.
    assert r.status_code in (201, 409), r.text
    with real.Session() as db:
        recorded = recorded_preset_digests(db)
    if r.status_code == 201:
        assert ("stroke-lnm", "ct-normalisation", "1.2.0") in recorded
        changed = tmp_path / "pipelines"
        (changed / "stroke-lnm").mkdir(parents=True)
        for name in ("pipeline.yaml",):
            (changed / "stroke-lnm" / name).write_bytes(
                (ROOT / "pipelines" / "stroke-lnm" / name).read_bytes()
            )
        presets_dir = changed / "stroke-lnm" / "presets"
        presets_dir.mkdir()
        raw = (ROOT / "pipelines/stroke-lnm/presets/ct-normalisation.yaml").read_bytes()
        (presets_dir / "ct-normalisation.yaml").write_bytes(raw + b"\n# edited\n")
        registry = DirectoryPresetRegistry(
            changed, real.services.pipelines, real.services.steps, recorded
        )
        assert registry.presets("stroke-lnm", "0.4.0") == []


def test_api_wires_the_preset_registry_with_recorded_digests(
    client, tmp_path, monkeypatch
):
    """The default `Orchestration` of the API reads presets next to the pipelines and
    gives the registry the digests recorded by earlier runs (rule P8)."""
    monkeypatch.setenv("PIPELINES_DIR", str(ROOT / "pipelines"))
    monkeypatch.setenv("STEP_MANIFESTS_DIR", str(ROOT / "steps"))
    monkeypatch.setenv("BUNDLE_ROOT", str(tmp_path / "bundles"))
    monkeypatch.delenv("PRESETS_DIR", raising=False)
    calls = []

    import app.db.session as session_module

    class Session:
        def __init__(self):
            self.inner = client.app.state.testing_session_local()

        def __enter__(self):
            return self.inner

        def __exit__(self, *exc):
            self.inner.close()

    monkeypatch.setattr(session_module, "SessionLocal", Session)
    import orchestrator.resolution as resolution

    original = resolution.recorded_preset_digests
    monkeypatch.setattr(
        resolution,
        "recorded_preset_digests",
        lambda db: calls.append(db) or {("stroke-lnm", "full", "1.2.0"): "0" * 64},
    )
    deps._default_orchestration.cache_clear()
    try:
        orch = deps._default_orchestration()
        assert calls, "the recorded digests were not read"
        names = {p.id for p in orch.presets.presets("stroke-lnm", "0.4.0")}
        # `full` is refused: its file differs from the digest recorded for its version.
        assert names == {"mni-mask-analysis", "ct-normalisation", "lesion-qc-report"}
        monkeypatch.setattr(resolution, "recorded_preset_digests", original)
        deps._default_orchestration.cache_clear()
        names = {
            p.id
            for p in deps._default_orchestration().presets.presets(
                "stroke-lnm", "0.4.0"
            )
        }
        assert names == {
            "full",
            "mni-mask-analysis",
            "ct-normalisation",
            "lesion-qc-report",
        }
        # PRESETS_DIR points elsewhere: nothing is offered.
        monkeypatch.setenv("PRESETS_DIR", str(tmp_path / "none"))
        deps._default_orchestration.cache_clear()
        assert (
            deps._default_orchestration().presets.presets("stroke-lnm", "0.4.0") == []
        )
    finally:
        deps._default_orchestration.cache_clear()


# --------------------------------------------------------------------- OpenAPI


def test_openapi_documents_the_plan_endpoints(client):
    schema = build_openapi(app)
    paths = schema["paths"]
    assert "get" in paths["/api/v1/pipelines/{pipeline_id}/versions/{version}/presets"]
    assert "post" in paths["/api/v1/pipelines/{pipeline_id}/versions/{version}/plan"]
    assert "get" in paths["/api/v1/admin/presets"]
    assert "put" in paths["/api/v1/admin/presets/{pipeline_id}/{preset_id}"]
    assert "get" in paths["/api/v1/config"]
    create = schema["components"]["schemas"]["RunCreate"]["properties"]
    assert {"preset", "targets"} <= set(create)
    assert "plan" in schema["components"]["schemas"]["RunDetailRead"]["properties"]
