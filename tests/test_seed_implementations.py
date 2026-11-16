"""Seeding of slot implementation settings and the pipeline wiring of the api (card F0-30a)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests import pgdb
from app.api.v1 import deps
from app.cli import seed_implementations as seed
from app.models.reference_bundle import SlotImplementationSetting
from tests.test_api import auth_headers

ROOT = Path(__file__).resolve().parent.parent
PIPELINES = ROOT / "pipelines"
COMPOSE = ROOT / "docker-compose.yml"


def _registry_tool():
    spec = importlib.util.spec_from_file_location(
        "stroke_lnm_registry", PIPELINES / "stroke-lnm" / "registry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def factory(tmp_path):
    engine = pgdb.new_engine(tmp_path, "seed.db")
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def run(factory, *args, pipelines=PIPELINES):
    return seed.main(
        ["--pipelines-dir", str(pipelines), *args], session_factory=factory
    )


def rows(factory):
    with factory() as db:
        return {
            (r.slot, r.implementation_id): (r.enabled, r.is_default)
            for r in db.scalars(select(SlotImplementationSetting))
        }


def registry():
    return yaml.safe_load((PIPELINES / "stroke-lnm" / "registry.yaml").read_text())


def test_profile_settings_match_the_registry_helper():
    tool = _registry_tool()
    reg = registry()
    for profile in ("deployment", "ci"):
        assert seed.profile_settings(reg, profile) == tool.enabled_settings(
            reg, profile
        )


def test_deployment_enables_real_entries_only(factory, capsys):
    assert run(factory, "--profile", "deployment") == 0
    got = rows(factory)
    reg = registry()
    kinds = {
        (slot, impl): e["kind"]
        for slot, impls in reg["implementations"].items()
        for impl, e in impls.items()
    }
    assert set(got) == set(kinds)
    for key, (enabled, _default) in got.items():
        if kinds[key] in ("stub", "planned"):
            assert not enabled, key
    assert got[("lesion-intake", "lesion-qc")] == (True, True)
    assert got[("report", "research-report")] == (True, True)
    assert got[("report", "research-report-es")] == (True, False)
    assert got[("functional-disconnection", "fdisconnection-stub")] == (False, False)
    assert got[("normalisation", "ct-lesion-to-mni152")] == (False, False)
    out = capsys.readouterr().out
    assert "placeholder" in out  # digests are not published yet
    assert "insert" in out and "committed" in out


def test_ci_profile_enables_the_stub(factory):
    assert run(factory, "--profile", "ci") == 0
    got = rows(factory)
    assert got[("functional-disconnection", "fdisconnection-stub")] == (True, True)
    assert got[("normalisation", "ct-lesion-to-mni152")] == (False, False)


def test_seeding_is_idempotent(factory, capsys):
    run(factory, "--profile", "deployment")
    first = rows(factory)
    capsys.readouterr()
    assert run(factory, "--profile", "deployment") == 0
    out = capsys.readouterr().out
    assert rows(factory) == first
    assert "0 inserted, 0 updated" in out


def test_dry_run_writes_nothing(factory, capsys):
    assert run(factory, "--profile", "deployment", "--dry-run") == 0
    assert rows(factory) == {}
    assert "dry run" in capsys.readouterr().out


def test_admin_changes_survive_a_rerun_and_reset_overwrites_them(factory, capsys):
    run(factory, "--profile", "deployment")
    with factory() as db:
        by_impl = {
            r.implementation_id: r
            for r in db.scalars(select(SlotImplementationSetting))
        }
        by_impl["research-report"].is_default = False
        db.flush()  # free the one-default-per-slot index first
        by_impl["research-report-es"].is_default = True
        db.flush()
        for r in db.scalars(select(SlotImplementationSetting)):
            if r.implementation_id == "fdisconnection-stub":
                r.enabled = True
            if r.implementation_id == "lnm-tracts":
                r.enabled, r.is_default = False, False
        db.commit()
    admin_state = rows(factory)
    capsys.readouterr()

    assert run(factory, "--profile", "deployment") == 0
    assert rows(factory) == admin_state
    assert "admin change" in capsys.readouterr().out

    assert run(factory, "--profile", "deployment", "--reset") == 0
    reset = rows(factory)
    assert reset[("report", "research-report")] == (True, True)
    assert reset[("report", "research-report-es")] == (True, False)
    assert reset[("functional-disconnection", "fdisconnection-stub")] == (False, False)
    assert reset[("tract-disconnection", "lnm-tracts")] == (True, True)
    # reset is itself idempotent
    capsys.readouterr()
    run(factory, "--profile", "deployment", "--reset")
    assert "0 updated" in capsys.readouterr().out


def test_missing_rows_do_not_steal_an_admin_default(factory):
    with factory() as db:
        db.add(
            SlotImplementationSetting(
                pipeline_id="stroke-lnm",
                slot="report",
                implementation_id="research-report-es",
                enabled=True,
                is_default=True,
            )
        )
        db.commit()
    assert run(factory, "--profile", "deployment") == 0
    got = rows(factory)
    assert got[("report", "research-report-es")] == (True, True)
    assert got[("report", "research-report")] == (True, False)


@pytest.fixture()
def pipelines_copy(tmp_path):
    target = tmp_path / "pipelines"
    shutil.copytree(
        PIPELINES / "stroke-lnm",
        target / "stroke-lnm",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    return target


@pytest.mark.parametrize(
    ("slot", "impl"),
    [
        ("functional-disconnection", "fdisconnection-stub"),  # stub
        ("normalisation", "ct-lesion-to-mni152"),  # planned
    ],
)
def test_deployment_profile_with_stub_or_planned_is_refused(
    factory, pipelines_copy, capsys, slot, impl
):
    path = pipelines_copy / "stroke-lnm" / "registry.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["profiles"]["deployment"]["enabled"][slot] = [impl]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert run(factory, "--profile", "deployment", pipelines=pipelines_copy) == 1
    assert "refused" in capsys.readouterr().err
    assert rows(factory) == {}


def test_planned_is_refused_in_ci_too(factory, pipelines_copy):
    path = pipelines_copy / "stroke-lnm" / "registry.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["profiles"]["ci"]["enabled"]["normalisation"] = ["ct-lesion-to-mni152"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert run(factory, "--profile", "ci", pipelines=pipelines_copy) == 1


def test_missing_pipelines_dir_is_an_error(factory, tmp_path, capsys):
    assert run(factory, "--profile", "deployment", pipelines=tmp_path / "nope") == 1
    assert "no registry.yaml" in capsys.readouterr().err


# --------------------------------------------- api without the pipelines profile


def test_api_lists_no_pipelines_when_directories_are_missing(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "missing-pipelines"))
    monkeypatch.setenv("STEP_MANIFESTS_DIR", str(tmp_path / "missing-steps"))
    monkeypatch.setenv("BUNDLE_ROOT", str(tmp_path / "missing-bundles"))
    deps._default_orchestration.cache_clear()
    try:
        headers, _ = auth_headers(client, "alice@example.org", "secret-pass")
        listing = client.get("/api/v1/pipelines", headers=headers)
        assert listing.status_code == 200
        assert listing.json() == []
        slots = client.get(
            "/api/v1/pipelines/stroke-lnm/versions/0.1.0/slots", headers=headers
        )
        assert slots.status_code == 404
        assert slots.json()["code"] == "pipeline_not_found"
    finally:
        deps._default_orchestration.cache_clear()


def test_compose_api_is_inert_without_pipeline_settings():
    api = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["api"]
    env = api["environment"]
    # The shared PIPELINES_DIR of the pipeline-worker must not leak into the api.
    assert env["PIPELINES_DIR"] == "${API_PIPELINES_DIR:-/app/pipelines-disabled}"
    assert env["STEP_MANIFESTS_DIR"] == "${API_STEP_MANIFESTS_DIR:-/app/steps-disabled}"
    for key in ("BUNDLE_ROOT", "DEPLOYMENT_PROFILE", "PLATFORM_VERSION"):
        assert key in env
    assert "./pipelines:/app/pipelines:ro" in api["volumes"]
    assert "./steps:/app/steps:ro" in api["volumes"]
    assert "profiles" not in api


def test_env_example_declares_api_pipeline_settings():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("API_PIPELINES_DIR", "API_STEP_MANIFESTS_DIR", "DEPLOYMENT_PROFILE"):
        assert key in text
