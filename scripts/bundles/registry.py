#!/usr/bin/env python3
"""CLI of the reference bundle registry (card F0-18).

    python scripts/bundles/registry.py --root /srv/bundles register PATH
    python scripts/bundles/registry.py --root /srv/bundles verify [ID VERSION] [--quick]
    python scripts/bundles/registry.py --root /srv/bundles list [--type T] [--json]
    python scripts/bundles/registry.py --root /srv/bundles resolve steps/<step>/step.yaml

`--root` defaults to $BUNDLE_ROOT. Thin wrapper over `orchestrator.bundles`.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "backend")]

from orchestrator.bundles import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
