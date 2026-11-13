"""The docker executor against a real Docker daemon, through the restricted socket proxy.

Skipped unless both variables are set (the ``docker-executor`` CI job sets them):

``STROKE_TEST_DOCKER_PROXY_URL``  base URL of the socket proxy, e.g. ``http://127.0.0.1:2375``
``STROKE_TEST_STEP_IMAGE``        pinned ``repository@sha256:<digest>`` of the template step
                                  image, already present on the Docker host (built from
                                  steps/template/Dockerfile and pushed, so that it has a digest)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import jsonschema
import nibabel as nib
import numpy as np
import pytest

from orchestrator.executors import (
    Accounting,
    DockerExecutor,
    ExecutorRefused,
    Outcome,
    Resources,
    StepInvocation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROXY_URL = os.environ.get("STROKE_TEST_DOCKER_PROXY_URL")
STEP_IMAGE = os.environ.get("STROKE_TEST_STEP_IMAGE")

pytestmark = pytest.mark.skipif(
    not (PROXY_URL and STEP_IMAGE),
    reason="needs STROKE_TEST_DOCKER_PROXY_URL and STROKE_TEST_STEP_IMAGE",
)

sys.path.insert(0, str(REPO_ROOT / "steps/template/tests"))


@pytest.fixture()
def executor() -> DockerExecutor:
    return DockerExecutor(
        allowed_images=[STEP_IMAGE],
        base_url=PROXY_URL,
        kill_grace_seconds=5,
    )


def make_invocation(tmp_path: Path, mask: Path, **overrides) -> StepInvocation:
    values = dict(
        step_run_id="it-1",
        step_name="template-lesion-volume",
        step_version="1.0.0",
        resources=Resources(cpus=1, memory_mb=512, wall_time_minutes=2),
        accounting=Accounting(submitted_by="service:integration-test"),
        work_dir=tmp_path / "attempt-1",
        image=STEP_IMAGE,
        inputs={"lesion_mask.nii.gz": mask},
        params={},
    )
    values.update(overrides)
    return StepInvocation(**values)


def execution_validator() -> jsonschema.Draft202012Validator:
    full = json.loads(
        (REPO_ROOT / "contracts/schemas/v1/provenance.schema.json").read_text(
            encoding="utf-8"
        )
    )
    return jsonschema.Draft202012Validator(
        {
            "$schema": full["$schema"],
            "$defs": full["$defs"],
            "$ref": "#/$defs/execution",
        }
    )


def test_stub_step_runs_end_to_end_through_the_proxy(tmp_path, executor):
    import make_reference

    data = tmp_path / "data"
    data.mkdir()
    expected = make_reference.generate(
        data, REPO_ROOT / "contracts/artefact-types.yaml"
    )

    handle = executor.submit(make_invocation(tmp_path, data / "lesion_mask.nii.gz"))
    status = executor.wait(handle, poll_interval=0.5, timeout=120)
    assert status.outcome is Outcome.SUCCEEDED, status
    assert status.exit_code == 0

    result = executor.collect(handle)
    assert result.succeeded
    assert sorted(result.outputs) == ["lesion_qc.json"]
    measured = json.loads(result.outputs["lesion_qc.json"].read_text(encoding="utf-8"))
    assert measured["n_voxels"] == expected["n_voxels"]
    assert (
        result.provenance is not None and result.provenance["kind"] == "StepProvenance"
    )
    assert result.log_path.read_text(encoding="utf-8").strip()  # logs were captured

    assert result.execution["executor"] == "docker"
    assert result.execution["image"] == STEP_IMAGE
    assert result.execution["external_job_id"] == handle.external_job_id
    assert not list(execution_validator().iter_errors(result.execution))

    # The state is persisted: a second executor object (a restarted worker) sees it.
    restarted = DockerExecutor(allowed_images=[STEP_IMAGE], base_url=PROXY_URL)
    assert restarted.poll(handle).outcome is Outcome.SUCCEEDED
    assert restarted.collect(handle).outputs.keys() == result.outputs.keys()

    executor.cleanup(handle)
    assert not (Path(handle.data["work_dir"]) / "out").exists()
    assert result.log_path.exists()


def test_invalid_input_is_rejected_with_exit_10(tmp_path, executor):
    import make_reference

    shape, affine = make_reference.working_grid(
        REPO_ROOT / "contracts/artefact-types.yaml"
    )
    mask = tmp_path / "not_binary.nii.gz"
    nib.save(nib.Nifti1Image(np.full(shape, 2, dtype=np.uint8), affine), str(mask))

    handle = executor.submit(make_invocation(tmp_path, mask))
    executor.wait(handle, poll_interval=0.5, timeout=120)
    result = executor.collect(handle)
    assert result.outcome is Outcome.REJECTED and result.exit_code == 10
    assert result.error is not None
    executor.cleanup(handle)


def test_image_outside_the_allow_list_is_refused(tmp_path, executor):
    other = "registry.example.org/steps/other@sha256:" + "0" * 64
    mask = tmp_path / "mask.nii.gz"
    mask.write_bytes(b"x")
    with pytest.raises(ExecutorRefused, match="allow-list"):
        executor.submit(make_invocation(tmp_path, mask, image=other))
    assert not (tmp_path / "attempt-1").exists()


def test_unknown_digest_is_refused_even_when_allow_listed(tmp_path):
    ghost = STEP_IMAGE.split("@")[0] + "@sha256:" + "0" * 64
    executor = DockerExecutor(allowed_images=[ghost], base_url=PROXY_URL)
    mask = tmp_path / "mask.nii.gz"
    mask.write_bytes(b"x")
    with pytest.raises(ExecutorRefused):
        executor.submit(make_invocation(tmp_path, mask, image=ghost))
