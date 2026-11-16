"""The orchestrator's view of reference bundles: the ``BundleResolver`` protocol.

The orchestrator never reads the bundle root itself. It asks a ``BundleResolver``
to turn a step's bundle requirements (``step.yaml`` ``bundles[]``) into verified
host directories, twice per selected slot:

1. at run creation, with the pins of the pipeline implementation entry, to choose
   the exact bundle versions; the orchestrator records them in
   ``run_slot_bundles`` (docs/data-model.md 4.6);
2. before every attempt, with exactly the recorded versions pinned, to re-check
   them (bundles.md 4.2: manifest unchanged, files present with listed sizes)
   and obtain the paths it mounts read-only at ``/ref/<name>/``.

Contract of ``resolve(requirements, selection)``:

- ``requirements``: the manifest's ``bundles`` entries ``{name, type, id?,
  version?, required?}``;
- ``selection``: ``{name: (bundle_id, version)}`` pins;
- returns ``{name: ResolvedBundle}`` (``orchestrator.bundles.ResolvedBundle``:
  ``bundle_id``, ``version``, ``content_digest``, ``host_path``); optional
  requirements that nothing satisfies are omitted;
- raises ``orchestrator.bundles.BundleError`` (``BundleNotFoundError`` when a
  required bundle is missing, withdrawn or out of range,
  ``BundleVerificationError`` when one fails verification). The orchestrator
  turns that into a refused run (creation) or a failed attempt with
  ``failure_code = 'bundle_unavailable'`` (step-contract.md 1.5 item 3).

``RegistryBundleResolver`` implements it on top of the F0-18 registry
(``orchestrator/bundles.py``) with the ``reference_bundles`` table as store. It
opens its own database session because ``SqlAlchemyStore.save`` commits, and the
orchestrator must not have its transaction committed from underneath it.
Tests use any object with the same ``resolve`` method.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from orchestrator.bundles import (
    BundleError,
    BundleRegistry,
    ResolvedBundle,
    SqlAlchemyStore,
)
from orchestrator.executors.base import BundleMount

FAILURE_CODE = "bundle_unavailable"

__all__ = [
    "BundleError",
    "BundleResolver",
    "FAILURE_CODE",
    "RegistryBundleResolver",
    "ResolvedBundle",
    "to_mount",
]


@runtime_checkable
class BundleResolver(Protocol):
    def resolve(
        self,
        requirements: Sequence[Mapping[str, Any]],
        selection: Mapping[str, tuple[str, str]] | None = None,
    ) -> Mapping[str, ResolvedBundle]: ...


class RegistryBundleResolver:
    """``BundleResolver`` backed by ``BundleRegistry(root, SqlAlchemyStore(session))``."""

    def __init__(self, root: Path | str, session_factory: Callable[[], Any]) -> None:
        self.root = Path(root)
        self.session_factory = session_factory

    def resolve(
        self,
        requirements: Sequence[Mapping[str, Any]],
        selection: Mapping[str, tuple[str, str]] | None = None,
    ) -> Mapping[str, ResolvedBundle]:
        if not requirements:
            return {}
        session = self.session_factory()
        try:
            registry = BundleRegistry(self.root, SqlAlchemyStore(session))
            return registry.resolve({"bundles": list(requirements)}, selection or {})
        finally:
            session.close()


def to_mount(resolved: ResolvedBundle) -> BundleMount:
    """Executor mount for a resolved bundle (``/ref/<name>/``, read-only)."""
    return BundleMount(
        name=resolved.name,
        host_path=resolved.host_path,
        id=resolved.bundle_id,
        version=resolved.version,
        content_digest=resolved.content_digest,
    )
