"""Registry rules for wrappers that consume an author's repository (card STEPS-01).

A wrapper of this repository names the author's repository and commit in
``source.upstream``; the step directory's ``upstream.env`` (read by its Dockerfile and by CI)
must say the same, and a step directory with an ``upstream.env`` needs the field.
``registry.py fetch`` fetches those repositories next to the external step sources.
"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERE = REPO / "pipelines" / "stroke-lnm"
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("stroke_lnm_registry_steps01", HERE / "registry.py")
registry_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry_tool
_spec.loader.exec_module(registry_tool)

WRAPPERS = {
    ("cognet-profile", "cognet-stroke"): "cognet-stroke",
    ("tract-disconnection", "lnm-tracts"): "lnm-tracts",
    ("normalisation", "ct-lesion-to-mni152"): "ct-lesion-to-mni152",
}


@pytest.fixture()
def docs():
    return registry_tool.load(HERE)


@pytest.fixture()
def root(tmp_path):
    """The three wrapper directories (upstream.env only) under a root a test may damage."""
    for step in WRAPPERS.values():
        (tmp_path / "steps" / step).mkdir(parents=True)
        shutil.copy(REPO / "steps" / step / "upstream.env", tmp_path / "steps" / step / "upstream.env")
    return tmp_path


def upstream_errors(registry, root):
    return [
        e
        for slot, impls in registry["implementations"].items()
        for impl_id, entry in impls.items()
        for e in registry_tool.upstream_errors(slot, impl_id, entry, root=root)
    ]


def test_committed_registry_agrees_with_the_steps(docs):
    assert registry_tool.check(*docs, registry_tool.load_presets(HERE)) == []
    for (slot, impl_id), step in WRAPPERS.items():
        upstream = docs[1]["implementations"][slot][impl_id]["source"]["upstream"]
        env = registry_tool.read_upstream_env(REPO / "steps" / step / "upstream.env")
        assert upstream == {"repository": env["UPSTREAM_REPOSITORY"], "commit": env["UPSTREAM_COMMIT"]}


def test_a_different_commit_fails(docs, root):
    registry = copy.deepcopy(docs[1])
    registry["implementations"]["cognet-profile"]["cognet-stroke"]["source"]["upstream"]["commit"] = "a" * 40
    found = upstream_errors(registry, root)
    assert any("cognet-stroke: source.upstream differs" in e for e in found), found


def test_upstream_env_without_the_field_fails(docs, root):
    registry = copy.deepcopy(docs[1])
    del registry["implementations"]["tract-disconnection"]["lnm-tracts"]["source"]["upstream"]
    found = upstream_errors(registry, root)
    assert any("lnm-tracts: steps/lnm-tracts/upstream.env exists" in e for e in found), found


def test_field_without_upstream_env_and_bad_values_fail(docs, root):
    (root / "steps" / "ct-lesion-to-mni152" / "upstream.env").unlink()
    registry = copy.deepcopy(docs[1])
    found = upstream_errors(registry, root)
    assert any("ct-lesion-to-mni152: source.upstream needs upstream.env" in e for e in found), found
    upstream = registry["implementations"]["cognet-profile"]["cognet-stroke"]["source"]["upstream"]
    upstream.update(repository="not a repository", commit="f98dbc8")
    found = upstream_errors(registry, root)
    assert any("repository must be <owner>/<name>" in e for e in found), found
    assert any("40-hex commit" in e for e in found), found


def test_fetch_targets_are_the_authors_repositories(docs):
    assert registry_tool.fetch_targets(docs[1]) == {
        "compneurobilbao/CTLesion2MNI152": "07702382b588f430b52005b61e71a9b48f5fcd15",
        "compneurobilbao/lnm": "123f3298b23dafa02a67b6f096790cfe3b775fb2",
        "compneurobilbao/COGNET-STROKE": "f98dbc84eae9ffaca03dc373f8a57d0b2b5e344e",
    }


def test_one_repository_at_two_commits_is_refused(docs):
    registry = copy.deepcopy(docs[1])
    entry = registry["implementations"]["tract-disconnection"]["lnm-tracts-hcp1065"]
    # new objects: the YAML merge key shares the nested mappings with lnm-tracts
    entry["source"] = dict(entry["source"], upstream=dict(entry["source"]["upstream"], commit="b" * 40))
    entry["step"] = {
        "name": "lnm-tracts",
        "version": "9.9.9",
    }
    with pytest.raises(SystemExit, match="two commits"):
        registry_tool.fetch_targets(registry)
