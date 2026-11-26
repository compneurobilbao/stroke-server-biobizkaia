"""CI check of the provenance of the slnm-coarse reference run (Docker or Apptainer).

The synthetic reference case has two parcels without a valid estimate, so the run must record
the warning unsupported_parcels; the software list must name the author's code and ANTs.

    python steps/slnm-coarse/ci/check_provenance.py PROVENANCE_JSON
"""

from __future__ import annotations

import json
import sys


def main(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        prov = json.load(fh)
    codes = {w["code"] for w in prov.get("warnings", [])}
    assert "unsupported_parcels" in codes, f"warnings: {sorted(codes)}"
    names = {s["name"] for s in prov.get("software", [])}
    for expected in ("fast-slnm (slnm-coarse)", "ANTs antsApplyTransforms"):
        assert expected in names, f"software: {sorted(names)}"
    print(f"provenance ok: warnings {sorted(codes)}, software {sorted(names)}")


if __name__ == "__main__":
    main(sys.argv[1])
