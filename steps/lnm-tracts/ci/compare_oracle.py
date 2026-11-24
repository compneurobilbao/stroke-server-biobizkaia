"""Compare the step's tables with the numpy oracle of tests/make_reference.py.

    python step/ci/compare_oracle.py RESULTS_DIR EXPECTED_DIR

Both directories hold <case>.tsv. Fractions must agree to 1e-6, counts exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from stroke_step_sdk import reftest

results, expected = Path(sys.argv[1]), Path(sys.argv[2])
bad = 0
for exp in sorted(expected.glob("*.tsv")):
    problem = reftest.compare_allclose(results / exp.name, exp, rtol=0, atol=1e-6)
    print(exp.stem, "ok" if problem is None else f"MISMATCH: {problem}")
    bad += problem is not None
sys.exit(1 if bad or not list(expected.glob("*.tsv")) else 0)
