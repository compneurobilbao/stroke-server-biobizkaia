"""Fixtures for the SDK tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sdk_helpers import MANIFEST, Run
from stroke_step_sdk import contracts


@pytest.fixture
def run(tmp_path) -> Run:
    return Run(tmp_path, MANIFEST)


@pytest.fixture
def make_run(tmp_path):
    def factory(manifest: dict) -> Run:
        return Run(tmp_path, manifest)

    return factory


@pytest.fixture
def ref_dir(tmp_path) -> Path:
    """A /ref directory holding a valid bundle under the requirement name 'atlas'."""
    directory = tmp_path / "ref" / "atlas"
    directory.mkdir(parents=True)
    shutil.copyfile(
        contracts.contracts_dir() / "examples" / "bundle-toy-normative-connectome.yaml",
        directory / "bundle.yaml",
    )
    return tmp_path / "ref"
