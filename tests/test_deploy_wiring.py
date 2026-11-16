"""Deployment wiring of pipeline runs (card F0-15a): task routing and Compose config."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from worker.celery_app import PIPELINE_QUEUE, celery_app

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"


def _queue(task_name: str) -> str:
    route = celery_app.amqp.router.route({}, task_name)
    return route["queue"].name


def test_pipeline_tasks_route_to_pipelines_queue():
    assert PIPELINE_QUEUE == "pipelines"
    assert _queue("worker.tasks.run_pipeline") == "pipelines"
    assert _queue("worker.tasks.recover_pipeline_runs") == "pipelines"


def test_legacy_task_stays_on_default_queue():
    assert _queue("worker.tasks.process_study") == celery_app.conf.task_default_queue


def test_beat_schedules_recovery_task_that_is_routed():
    entry = celery_app.conf.beat_schedule["recover-pipeline-runs"]
    assert entry["task"] == "worker.tasks.recover_pipeline_runs"
    assert _queue(entry["task"]) == "pipelines"


def test_no_explicit_queue_list_so_default_worker_keeps_only_default_queue():
    # task_queues would make a worker without -Q consume every declared queue.
    assert not celery_app.conf.task_queues or [
        q.name for q in celery_app.conf.task_queues
    ] == [celery_app.conf.task_default_queue]


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_pipeline_services_are_behind_the_pipelines_profile(compose):
    for name in ("pipeline-worker", "beat", "docker-proxy"):
        assert "pipelines" in compose[name]["profiles"], name
    for name, svc in compose.items():
        if name not in ("pipeline-worker", "beat", "docker-proxy"):
            assert "profiles" not in svc, f"{name} must stay in the default stack"


def test_legacy_worker_is_unchanged_and_not_on_executor_net(compose):
    worker = compose["worker"]
    assert "-Q" not in worker["command"]
    assert "executor-net" not in worker.get("networks", [])


def test_pipeline_worker_consumes_only_pipelines_queue(compose):
    pw = compose["pipeline-worker"]
    assert "-Q pipelines" in pw["command"]
    assert pw["build"]["dockerfile"] == "worker/Dockerfile"
    assert set(pw["networks"]) == {"default", "executor-net"}
    assert "docker-proxy" in pw["depends_on"]
    env = pw["environment"]
    for key in (
        "DOCKER_PROXY_URL",
        "DOCKER_EXECUTOR_HOST_PATH_MAP",
        "BUNDLE_ROOT",
        "PIPELINES_DIR",
        "STEP_MANIFESTS_DIR",
        "ORCHESTRATOR_EXECUTOR",
        "ORCHESTRATOR_MAX_ATTEMPTS",
    ):
        assert key in env, key


def test_only_proxy_and_pipeline_worker_join_executor_net(compose):
    members = {n for n, s in compose.items() if "executor-net" in s.get("networks", [])}
    assert members == {"docker-proxy", "pipeline-worker"}


def test_beat_is_single_instance_without_a_worker_role(compose):
    beat = compose["beat"]
    assert " beat " in beat["command"]
    assert "worker " not in beat["command"].split("celery", 1)[1].replace(
        "worker.celery_app", ""
    )
    assert "deploy" not in beat and "scale" not in beat
    assert "executor-net" not in beat.get("networks", [])


def test_env_example_declares_pipeline_variables():
    keys = {
        line.split("=", 1)[0].lstrip("# ").strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    for key in (
        "DOCKER_PROXY_URL",
        "DOCKER_EXECUTOR_HOST_PATH_MAP",
        "BUNDLE_ROOT",
        "PIPELINES_DIR",
        "STEP_MANIFESTS_DIR",
        "ORCHESTRATOR_EXECUTOR",
        "ORCHESTRATOR_WORKER_CONCURRENCY",
    ):
        assert key in keys, key


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("profile_args", [[], ["--profile", "pipelines"]])
def test_compose_config_is_valid(profile_args):
    result = subprocess.run(
        ["docker", "compose", *profile_args, "config", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
