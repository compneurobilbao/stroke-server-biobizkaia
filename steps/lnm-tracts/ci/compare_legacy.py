"""Compare the step's tables with the output of the former lnm_tracts.sh.

    python step/ci/compare_legacy.py LEGACY_CSV RESULTS_DIR

LEGACY_CSV is data/lnm_tracts.csv written by the script (columns BIDS, Tract, Disc, with Disc
the surviving fraction truncated to three decimals by ``bc``). RESULTS_DIR holds
<BIDS>.tsv, the step's table for each lesion. Two checks per row:

* Disc equals the truncation to three decimals of the surviving fraction computed from the
  step's integer counts (this reproduces the script exactly);
* 1 - Disc is within 0.001 of the step's ``disconnection`` (the contract's definition).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def main() -> int:
    legacy = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
    results = Path(sys.argv[2])
    seen = set()
    failures = []
    for row in legacy:
        case, tract = row["BIDS"], row["Tract"]
        with open(results / f"{case}.tsv", encoding="utf-8", newline="") as fh:
            table = {r["tract"]: r for r in csv.DictReader(fh, delimiter="\t")}
        new = table[tract]
        seen.add((case, tract))
        total, cut = int(new["n_streamlines"]), int(new["n_disconnected"])
        disc = float(row["Disc"])
        truncated = ((total - cut) * 1000 // total) / 1000
        ok_exact = abs(disc - truncated) < 1e-9
        ok_contract = abs((1 - disc) - float(new["disconnection"])) < 0.001
        print(
            f"{case:6s} {tract:18s} Disc={row['Disc']:>6s}  step disconnection="
            f"{new['disconnection']} ({cut}/{total})  {'ok' if ok_exact and ok_contract else 'MISMATCH'}"
        )
        if not (ok_exact and ok_contract):
            failures.append((case, tract))
    expected = set()
    for p in results.glob("*.tsv"):
        with open(p, encoding="utf-8", newline="") as fh:
            expected |= {(p.stem, r["tract"]) for r in csv.DictReader(fh, delimiter="\t")}
    if seen != expected:
        print(f"row sets differ: {sorted(seen ^ expected)}")
        return 1
    print(f"{len(seen)} rows compared, {len(failures)} mismatches")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
