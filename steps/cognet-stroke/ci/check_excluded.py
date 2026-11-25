"""Check a container run of the cognet-stroke step on a map with NaN parcels (CI only).

    python steps/cognet-stroke/ci/check_excluded.py OUT_DIR

The profile validates against the contract schema and carries excluded_parcels with count above
zero and reason undefined_in_disconnection_map; the provenance records the warning
parcels_excluded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

out = Path(sys.argv[1])
schema = json.loads(Path("contracts/schemas/v1/cognet-profile.schema.json").read_text(encoding="utf-8"))
profile = json.loads((out / "profile.json").read_text(encoding="utf-8"))
Draft202012Validator(schema).validate(profile)
excluded = profile["excluded_parcels"]
assert excluded["count"] > 0, excluded
assert excluded["reason"] == "undefined_in_disconnection_map", excluded
assert excluded["fraction"] == excluded["count"] / profile["method"]["n_parcels"], excluded
warnings = json.loads((out / "provenance.json").read_text(encoding="utf-8")).get("warnings", [])
assert warnings and warnings[0]["code"] == "parcels_excluded", warnings
print("excluded_parcels:", excluded)
