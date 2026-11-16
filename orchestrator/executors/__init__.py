"""Executors run one step attempt on a backend (ADR-0003).

``base`` defines the interface and the data types shared by every backend;
``subprocess`` runs a local command (locally installed steps and legacy adapters);
``docker`` runs a sibling container through a restricted socket proxy.
"""

from orchestrator.executors.base import (
    Accounting,
    BundleMount,
    Executor,
    ExecutorError,
    ExecutorHandle,
    ExecutorRefused,
    JobState,
    JobStatus,
    Outcome,
    Resources,
    StepInvocation,
    StepResult,
    SubmitAs,
    Usage,
    classify_exit,
)
from orchestrator.executors.docker import DockerExecutor
from orchestrator.executors.subprocess import SubprocessExecutor

__all__ = [
    "Accounting",
    "BundleMount",
    "DockerExecutor",
    "Executor",
    "ExecutorError",
    "ExecutorHandle",
    "ExecutorRefused",
    "JobState",
    "JobStatus",
    "Outcome",
    "Resources",
    "StepInvocation",
    "StepResult",
    "SubmitAs",
    "SubprocessExecutor",
    "Usage",
    "classify_exit",
]
