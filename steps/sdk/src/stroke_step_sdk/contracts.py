"""Access to the contract artefacts (JSON Schemas, registry, validator).

Nothing is copied: the schemas, ``artefact-types.yaml`` and ``validate.py`` are read from
the repository's ``contracts/`` directory. Lookup order: the ``STEP_CONTRACTS_DIR``
environment variable (set in step images), then the nearest ``contracts/`` directory
above this file (development checkout).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import yaml

CONTRACTS_ENV = "STEP_CONTRACTS_DIR"
SUPPORTED_CONTRACT_VERSION = "1.0"


def _looks_like_contracts(path: Path) -> bool:
    return (path / "schemas" / "v1" / "step.schema.json").is_file() and (
        path / "validate.py"
    ).is_file()


@lru_cache(maxsize=1)
def contracts_dir() -> Path:
    env = os.environ.get(CONTRACTS_ENV)
    if env:
        path = Path(env)
        if not _looks_like_contracts(path):
            raise RuntimeError(
                f"{CONTRACTS_ENV} does not point to a contracts directory"
            )
        return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if _looks_like_contracts(candidate):
            return candidate
    raise RuntimeError(
        f"contracts directory not found; set {CONTRACTS_ENV} (step images copy it to /opt/step/contracts)"
    )


@lru_cache(maxsize=1)
def validator() -> ModuleType:
    """The contract validator module (``contracts/validate.py``), loaded from its path."""
    path = contracts_dir() / "validate.py"
    name = "_contracts_validate"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses in the module need this
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def registry() -> dict:
    """The artefact type registry document (``types`` and ``grids`` keys)."""
    path = validator().DEFAULT_TYPES_PATH
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
