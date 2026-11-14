"""Build the worker's ``Orchestrator`` from the environment (card F0-15).

Variables (all optional; documented in docs/processing-pipeline.md):

| Variable | Default | Meaning |
|---|---|---|
| ``PIPELINES_DIR`` | ``/app/pipelines`` | Tree scanned for ``pipeline.yaml`` (F0-33 fills it). |
| ``STEP_MANIFESTS_DIR`` | ``/app/steps`` | Tree scanned for ``step.yaml`` (interim, until F0-33 reads manifests from images). |
| ``BUNDLE_ROOT`` | ``/app/data/bundles`` | Reference bundle root (F0-18). |
| ``ORCHESTRATOR_EXECUTOR`` | ``docker`` | Default executor: ``docker`` or ``subprocess``. |
| ``ORCHESTRATOR_STEP_EXECUTORS`` | empty | Per-step overrides, ``step-name=executor,...``. |
| ``ORCHESTRATOR_POLL_SECONDS`` | ``5`` | Poll interval of running attempts. |
| ``ORCHESTRATOR_MAX_ATTEMPTS`` | ``2`` | Attempts per slot per start or resume (automatic retry). |
| ``ORCHESTRATOR_LEASE_SECONDS`` | ``600`` | Heartbeat age after which another task may take a run over. |
| ``ORCHESTRATOR_PIPELINE_UNAVAILABLE_GRACE_SECONDS`` | ``3600`` | How long live attempts of a run whose pipeline is not loaded are left alone before they are stopped and the run fails. |
| ``PLATFORM_VERSION`` | ``0.0.0-dev`` | Recorded on new runs by ``create_run`` callers. |

The docker executor's image allow-list is the set of images pinned in the loaded
pipelines (F0-14: "image allow-list from the pipeline registry").
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from orchestrator.bundle_resolver import RegistryBundleResolver
from orchestrator.catalog import DirectoryPipelineRegistry, DirectoryStepCatalog
from orchestrator.engine import ExecutorRegistry, Orchestrator, OrchestratorConfig
from orchestrator.store import RunStore


def config_from_env(env: Mapping[str, str] | None = None) -> OrchestratorConfig:
    env = os.environ if env is None else env
    return OrchestratorConfig(
        poll_seconds=float(env.get("ORCHESTRATOR_POLL_SECONDS", "5")),
        max_attempts=int(env.get("ORCHESTRATOR_MAX_ATTEMPTS", "2")),
        lease_seconds=int(env.get("ORCHESTRATOR_LEASE_SECONDS", "600")),
        pipeline_unavailable_grace_seconds=int(
            env.get("ORCHESTRATOR_PIPELINE_UNAVAILABLE_GRACE_SECONDS", "3600")
        ),
    )


def _per_step(text: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (p.strip() for p in text.split(","))):
        name, sep, executor = item.partition("=")
        if not sep:
            raise ValueError(f"invalid ORCHESTRATOR_STEP_EXECUTORS entry: {item!r}")
        out[name.strip()] = executor.strip()
    return out


def build_orchestrator(
    session_factory, storage_root: Path, settings=None, env=None
) -> Orchestrator:
    env = os.environ if env is None else env
    pipelines = DirectoryPipelineRegistry(env.get("PIPELINES_DIR", "/app/pipelines"))
    steps = DirectoryStepCatalog(env.get("STEP_MANIFESTS_DIR", "/app/steps"))
    allowed = (
        frozenset().union(*(p.images for p in pipelines.all()))
        if pipelines.all()
        else frozenset()
    )
    per_step = _per_step(env.get("ORCHESTRATOR_STEP_EXECUTORS", ""))
    wanted = {env.get("ORCHESTRATOR_EXECUTOR", "docker"), *per_step.values()}
    executors = {}
    if "docker" in wanted:
        from orchestrator.executors.docker import DockerExecutor

        executors["docker"] = DockerExecutor.from_env(allowed, environ=env)
    if "subprocess" in wanted:
        from orchestrator.executors.subprocess import SubprocessExecutor

        executors["subprocess"] = SubprocessExecutor()
    unknown = wanted - set(executors)
    if unknown:
        raise ValueError(f"unknown executors configured: {sorted(unknown)}")
    return Orchestrator(
        session_factory,
        pipelines=pipelines,
        steps=steps,
        executors=ExecutorRegistry(
            executors, env.get("ORCHESTRATOR_EXECUTOR", "docker"), per_step
        ),
        bundles=RegistryBundleResolver(
            env.get("BUNDLE_ROOT", "/app/data/bundles"), session_factory
        ),
        store=RunStore(storage_root),
        settings=settings,
        config=config_from_env(env),
    )
