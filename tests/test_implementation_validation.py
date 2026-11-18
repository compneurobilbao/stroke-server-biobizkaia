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
    assert levels.level("stroke-lnm", "functional-disconnection", "flnm-fast") == "reference-tested"
    assert levels.level("stroke-lnm", "lesion-intake", "lesion-qc") == "validated"
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
    assert CODE in err and "functional-disconnection/flnm-fast" in err
    assert rows(factory) == {}


def test_seed_skip_flag_seeds_the_refused_implementation_disabled(
    factory, monkeypatch, capsys
):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    assert run(factory, "--profile", "deployment", "--skip-below-minimum") == 0
    out = capsys.readouterr().out
    assert "warning" in out and CODE in out
    got = rows(factory)
    assert got[("functional-disconnection", "flnm-fast")] == (False, False)
    assert got[("lesion-intake", "lesion-qc")] == (True, True)
    assert got[("report", "research-report")] == (True, True)


def test_seed_on_the_lab_instance_enables_flnm_fast(factory, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "on-premise")
    assert run(factory, "--profile", "deployment") == 0
    assert rows(factory)[("functional-disconnection", "flnm-fast")] == (True, True)
    # raising the lab's minimum refuses it again
    monkeypatch.setenv("MIN_IMPLEMENTATION_VALIDATION", "validated")
    assert run(factory, "--profile", "deployment", "--reset") == 1


def test_seed_ci_profile_takes_unvalidated_whatever_the_deployment(factory, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "research-public")
    assert run(factory, "--profile", "ci") == 0
    assert rows(factory)[("functional-disconnection", "fdisconnection-stub")] == (True, True)


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
    assert "left as is" in out
    assert rows(factory)[("functional-disconnection", "flnm-fast")] == (True, True)
