"""Minimum validation level of enabled implementations (card F1-23, ADR-0018 owner decision 8,
run-plans.md 4.4): ``MIN_IMPLEMENTATION_VALIDATION``, the admin enable check and the seeding CLI
refuse an implementation below the level with ``implementation_not_validated``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.v1 import deps
from app.api.v1.deps import Orchestration
from app.core.validation import (
    DirectoryValidationLevels,
    StaticValidationLevels,
    implementation_level,
    min_implementation_validation,
)
from app.models import AuditEvent
from app.models.preset_setting import PresetSetting
from app.models.reference_bundle import SlotImplementationSetting
from tests.test_api_v1_plans import PIPE, make_synthetic  # noqa: F401  (fixtures below)
from tests import pgdb
from tests.test_seed_implementations import registry, rows, run

ROOT = Path(__file__).resolve().parent.parent
PIPELINES = ROOT / "pipelines"
CODE = "implementation_not_validated"


# ------------------------------------------------------------------ configuration


@pytest.mark.parametrize("profile", ["research-public", "mask-only"])
def test_public_profiles_require_validated(profile):
    assert min_implementation_validation(profile, {}) == "validated"
    assert min_implementation_validation(profile, {"MIN_IMPLEMENTATION_VALIDATION": ""}) == "validated"
    assert (
        min_implementation_validation(profile, {"MIN_IMPLEMENTATION_VALIDATION": "validated"})
        == "validated"
    )
    for lower in ("reference-tested", "unvalidated"):
        with pytest.raises(RuntimeError, match="must be validated"):
            min_implementation_validation(profile, {"MIN_IMPLEMENTATION_VALIDATION": lower})


def test_on_premise_defaults_to_reference_tested_and_is_configurable():
    assert min_implementation_validation("on-premise", {}) == "reference-tested"
    for value in ("unvalidated", "reference-tested", "validated"):
        assert (
            min_implementation_validation("on-premise", {"MIN_IMPLEMENTATION_VALIDATION": value})
            == value
        )


@pytest.mark.parametrize("profile", ["research-public", "on-premise"])
def test_unknown_level_is_refused(profile):
    with pytest.raises(RuntimeError, match="MIN_IMPLEMENTATION_VALIDATION must be one of"):
        min_implementation_validation(profile, {"MIN_IMPLEMENTATION_VALIDATION": "good"})


def test_the_api_refuses_to_start_below_validated_on_a_public_profile(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "reference-tested")
    with pytest.raises(RuntimeError, match="must be validated"):
        deps.effective_min_validation()
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")
    assert deps.effective_min_validation() == "reference-tested"


def test_level_rule_is_the_one_of_the_registry_tool():
    spec = importlib.util.spec_from_file_location(
        "stroke_lnm_registry_levels", PIPELINES / "stroke-lnm" / "registry.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tuple(tool.VALIDATION_LEVELS) == (
        "unvalidated",
        "reference-tested",
        "validated",
    )
    entries = [
        {"kind": "real"},
        {"kind": "real", "origin": "internal"},
        {"kind": "real", "origin": "external"},
        {"kind": "real", "origin": "external", "validation": {"status": "reference-tested"}},
        {"kind": "real", "validation": {"status": "validated"}},
        {"kind": "stub"},
        {"kind": "planned"},
        {"kind": "stub", "validation": {"status": "unvalidated"}},
    ]
    for impls in registry()["implementations"].values():
        entries += list(impls.values())
    for entry in entries:
        assert implementation_level(entry) == tool.validation_level(entry), entry


def test_directory_levels_read_the_committed_registry():
    levels = DirectoryValidationLevels(PIPELINES)
    assert levels.level("stroke-lnm", "functional-disconnection", "flnm-fast-hcp-aging") == "reference-tested"
    assert levels.level("stroke-lnm", "lesion-intake", "lesion-qc") == "reference-tested"
    assert (
        levels.level("stroke-lnm", "functional-disconnection", "fdisconnection-stub")
        == "unvalidated"
    )
    assert levels.level("stroke-lnm", "report", "nope") == "unvalidated"
    assert DirectoryValidationLevels(PIPELINES / "missing").level("a", "b", "c") == "unvalidated"


# ------------------------------------------------------------------ admin endpoint


def levels_for(syn, mapping):
    old = syn.services
    syn.services = Orchestration(
        old.pipelines,
        old.steps,
        old.bundles,
        old.platform_version,
        old.presets,
        StaticValidationLevels(
            {(PIPE, slot, impl): level for (slot, impl), level in mapping.items()},
            default="validated",
        ),
    )


def put(syn, slot, impl, **body):
    return syn.client.put(
        f"/api/v1/admin/implementations/{PIPE}/{slot}/{impl}",
        json={"enabled": True, **body},
        headers=syn.admin,
    )


def setting(syn, slot, impl):
    with syn.Session() as db:
        return db.scalar(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.pipeline_id == PIPE,
                SlotImplementationSetting.slot == slot,
                SlotImplementationSetting.implementation_id == impl,
            )
        )


def audit_count(syn):
    with syn.Session() as db:
        return len(
            list(
                db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "implementation_setting_changed"
                    )
                )
            )
        )


@pytest.fixture()
def syn(client, monkeypatch):
    env = make_synthetic(client, monkeypatch)
    levels_for(
        env,
        {
            ("disconnection", "disc"): "reference-tested",
            ("disconnection", "disc-alt"): "unvalidated",
        },
    )
    return env


def test_lab_default_enables_reference_tested_and_refuses_unvalidated(syn, monkeypatch):
    syn.policy("free_for_researchers", "on-premise")
    monkeypatch.delenv("MIN_IMPLEMENTATION_VALIDATION", raising=False)
    ok = put(syn, "disconnection", "disc", is_default=True)
    assert ok.status_code == 200, ok.text
    assert ok.json()["enabled"] is True
    refused = put(syn, "disconnection", "disc-alt")
    assert refused.status_code == 422
    body = refused.json()
    assert body["code"] == CODE and body["slot"] == "disconnection"
    assert "unvalidated" in body["detail"] and "reference-tested" in body["detail"]
    assert setting(syn, "disconnection", "disc-alt") is None
    # a validated one is fine, an unvalidated one may be disabled (never refused)
    assert put(syn, "intake", "intake").status_code == 200
    off = put(syn, "disconnection", "disc-alt", enabled=False)
    assert off.status_code == 200 and off.json()["enabled"] is False


def test_public_profiles_refuse_reference_tested(syn, monkeypatch):
    monkeypatch.delenv("MIN_IMPLEMENTATION_VALIDATION", raising=False)
    for profile in ("research-public", "mask-only"):
        syn.policy("presets_only", profile)
        refused = put(syn, "disconnection", "disc")
        assert (refused.status_code, refused.json()["code"]) == (422, CODE), profile
        assert "at least validated" in refused.json()["detail"]
        assert put(syn, "intake", "intake").status_code == 200


def test_the_minimum_can_be_raised_on_the_lab_instance(syn, monkeypatch):
    syn.policy("free_for_researchers", "on-premise")
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    assert put(syn, "disconnection", "disc").json()["code"] == CODE
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "unvalidated")
    assert put(syn, "disconnection", "disc-alt").status_code == 200


def test_a_refusal_changes_nothing(syn, monkeypatch):
    syn.policy("free_for_researchers", "on-premise")
    monkeypatch.delenv("MIN_IMPLEMENTATION_VALIDATION", raising=False)
    assert put(syn, "disconnection", "disc", is_default=True).status_code == 200
    before = audit_count(syn)
    refused = put(syn, "disconnection", "disc-alt", is_default=True)
    assert refused.status_code == 422
    assert audit_count(syn) == before
    row = setting(syn, "disconnection", "disc")
    assert (row.enabled, row.is_default) == (True, True)  # the default was not cleared


def test_an_implementation_without_registry_data_is_unvalidated(client, monkeypatch):
    syn = make_synthetic(client, monkeypatch)
    syn.services = Orchestration(
        syn.services.pipelines, syn.services.steps, syn.services.bundles, "x", syn.services.presets
    )
    syn.policy("free_for_researchers", "on-premise")
    monkeypatch.delenv("MIN_IMPLEMENTATION_VALIDATION", raising=False)
    assert put(syn, "intake", "intake").json()["code"] == CODE


# ------------------------------------------------------------------ seeding CLI


@pytest.fixture()
def factory(tmp_path):
    engine = pgdb.new_engine(tmp_path, "seed-validation.db")
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def test_seed_refuses_a_profile_below_the_minimum_on_a_public_deployment(
    factory, monkeypatch, capsys
):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    assert run(factory, "--profile", "deployment") == 1
    err = capsys.readouterr().err
    assert CODE in err and "functional-disconnection/flnm-fast-hcp-aging" in err
    assert rows(factory) == {}


def test_seed_skip_flag_seeds_the_refused_implementation_disabled(
    factory, monkeypatch, capsys
):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    assert run(factory, "--profile", "deployment", "--skip-below-minimum") == 0
    out = capsys.readouterr().out
    assert "warning" in out and CODE in out
    got = rows(factory)
    # nothing is validated yet: a public deployment enables no in-house step either
    assert all(enabled is False for enabled, _default in got.values())
    presets = preset_rows(factory)
    assert all(row == (False, False) for row in presets.values())
    assert "seeded disabled: no implementation is enabled for its required slot" in out


def test_seed_on_the_lab_instance_enables_flnm_fast(factory, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")
    assert run(factory, "--profile", "deployment") == 0
    assert rows(factory)[("functional-disconnection", "flnm-fast-hcp-aging")] == (True, True)
    # raising the lab's minimum refuses it again
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    assert run(factory, "--profile", "deployment", "--reset") == 1


def test_seed_ci_profile_takes_unvalidated_whatever_the_deployment(factory, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    assert run(factory, "--profile", "ci", "--allow-ci-profile") == 0
    assert rows(factory)[("functional-disconnection", "fdisconnection-stub")] == (True, True)


@pytest.mark.parametrize("profile", ["research-public", "mask-only"])
def test_seed_ci_profile_is_refused_on_public_deployments(factory, monkeypatch, capsys, profile):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", profile)
    assert run(factory, "--profile", "ci") == 1
    assert "--allow-ci-profile" in capsys.readouterr().err
    assert rows(factory) == {}
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")  # the lab and CI stacks need no override
    assert run(factory, "--profile", "ci") == 0


def test_seed_refuses_an_invalid_minimum(factory, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "unvalidated")
    assert run(factory, "--profile", "deployment") == 1
    assert "must be validated" in capsys.readouterr().err


def test_seed_warns_about_an_enabled_row_below_the_minimum(factory, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")
    assert run(factory, "--profile", "deployment") == 0
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    capsys.readouterr()
    assert run(factory, "--profile", "deployment", "--skip-below-minimum") == 0
    out = capsys.readouterr().out
    assert "left as is" in out and "WARNING" in out and "--reset" in out
    assert rows(factory)[("functional-disconnection", "flnm-fast-hcp-aging")] == (True, True)
    # --reset disables what is below the minimum
    assert run(factory, "--profile", "deployment", "--skip-below-minimum", "--reset") == 0
    assert all(enabled is False for enabled, _default in rows(factory).values())


def test_seed_disables_presets_without_an_enabled_required_slot(factory, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")
    assert run(factory, "--profile", "deployment") == 0
    # the lab's minimum preset: every required slot has an enabled implementation
    assert preset_rows(factory)["mni-mask-analysis"] == (True, True)
    capsys.readouterr()
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    assert run(factory, "--profile", "deployment", "--skip-below-minimum", "--reset") == 0
    assert preset_rows(factory)["mni-mask-analysis"] == (False, False)
    assert "preset" in capsys.readouterr().out


def preset_rows(factory):
    with factory() as db:
        return {
            r.preset_id: (r.enabled, r.is_default) for r in db.scalars(select(PresetSetting))
        }


# ------------------------------------------- plans, runs and listings ignore stale rows


def stale_enable(syn, slot, impl):
    """An enabled row below the minimum, as an older seed or a changed minimum leaves it."""
    with syn.Session() as db:
        row = db.scalar(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.pipeline_id == PIPE,
                SlotImplementationSetting.slot == slot,
                SlotImplementationSetting.implementation_id == impl,
            )
        )
        if row is None:
            row = SlotImplementationSetting(
                pipeline_id=PIPE, slot=slot, implementation_id=impl
            )
            db.add(row)
        row.enabled = True
        db.commit()


@pytest.fixture()
def lab(syn, monkeypatch):
    syn.policy("free_for_researchers", "on-premise")
    monkeypatch.delenv("MIN_IMPLEMENTATION_VALIDATION", raising=False)
    syn.enable_implementations(skip=("disc-alt",))
    stale_enable(syn, "disconnection", "disc-alt")
    return syn


SLOTS_URL = f"/api/v1/pipelines/{PIPE}/versions/1.0.0/slots"


def test_a_stale_enabled_row_is_not_enabled_for_plans_and_runs(lab):
    wanted = {"implementations": {"disconnection": "disc-alt"}}
    preview = lab.preview(["lesion_mni"], **wanted)
    assert preview.status_code == 200
    body = preview.json()
    assert body["feasible"] is False
    assert [r["code"] for r in body["reasons"]] == [CODE]
    assert body["reasons"][0]["slot"] == "disconnection"
    case_id = lab.case(["lesion_mni"])
    refused = lab.start(case_id, **wanted)
    assert (refused.status_code, refused.json()["code"]) == (422, CODE)
    assert lab.counts()["PipelineRun"] == 0
    # the validated and reference-tested ones still run, and the default is not the stale one
    ok = lab.start(case_id)
    assert ok.status_code == 201, ok.text
    slots = {s["slot"]: s for s in ok.json()["slots"]}
    assert slots["disconnection"]["implementation_id"] == "disc"


def test_slot_listing_shows_levels_and_hides_below_minimum(lab):
    slots = {
        s["name"]: s
        for s in lab.client.get(SLOTS_URL, headers=lab.alice).json()
    }
    disc = {i["id"]: i for i in slots["disconnection"]["implementations"]}
    assert set(disc) == {"disc"}  # the stale one is not offered
    assert disc["disc"]["validation_level"] == "reference-tested"
    assert disc["disc"]["experimental"] is True
    assert slots["intake"]["implementations"][0]["experimental"] is False
    assert slots["intake"]["implementations"][0]["validation_level"] == "validated"
    assert slots["disconnection"]["default_implementation"] == "disc"
    admin = {
        s["name"]: s
        for s in lab.client.get(
            SLOTS_URL, params={"include_disabled": "true"}, headers=lab.admin
        ).json()
    }
    alt = {i["id"]: i for i in admin["disconnection"]["implementations"]}["disc-alt"]
    assert alt["enabled"] is False
    assert alt["unavailable_reason"] == CODE and alt["validation_level"] == "unvalidated"


def test_admin_list_shows_level_experimental_and_below_minimum(lab):
    listing = {
        (r["slot"], r["implementation_id"]): r
        for r in lab.client.get("/api/v1/admin/implementations", headers=lab.admin).json()
    }
    assert listing[("disconnection", "disc")]["experimental"] is True
    assert listing[("disconnection", "disc")]["below_minimum"] is False
    alt = listing[("disconnection", "disc-alt")]
    assert (alt["validation_level"], alt["below_minimum"], alt["experimental"]) == (
        "unvalidated",
        True,
        False,
    )
    assert listing[("intake", "intake")]["validation_level"] == "validated"


def test_run_detail_records_the_level_in_effect_at_creation(lab, monkeypatch):
    case_id = lab.case(["lesion_mni"])
    run = lab.start(case_id)
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    got = lab.client.get(f"/api/v1/runs/{run_id}", headers=lab.alice).json()
    levels = {
        s["slot"]: (s["validation_level"], s["experimental"])
        for s in got["slots"]
        if s["status"] == "selected"
    }
    assert levels["disconnection"] == ("reference-tested", True)
    assert levels["intake"] == ("validated", False)
    # a later change of the registry does not rewrite the record
    levels_for(lab, {("disconnection", "disc"): "validated"})
    again = lab.client.get(f"/api/v1/runs/{run_id}", headers=lab.alice).json()
    assert {s["slot"]: s["validation_level"] for s in again["slots"] if s["status"] == "selected"}[
        "disconnection"
    ] == "reference-tested"
    # skipped slots carry no level
    assert all(s["validation_level"] is None for s in got["slots"] if s["status"] != "selected")


def test_raising_the_minimum_removes_reference_tested_from_plans(lab, monkeypatch):
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    preview = lab.preview(["lesion_mni"], implementations={"disconnection": "disc"}).json()
    assert preview["feasible"] is False
    assert preview["reasons"][0]["code"] == CODE
    slots = {s["name"]: s for s in lab.client.get(SLOTS_URL, headers=lab.alice).json()}
    assert slots["disconnection"]["implementations"] == []
    assert slots["disconnection"]["default_implementation"] is None
