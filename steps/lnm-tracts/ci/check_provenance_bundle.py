"""Check that a provenance record names the atlas bundle that was mounted.

    python step/ci/check_provenance_bundle.py PROVENANCE_JSON BUNDLE_YAML
"""

from __future__ import annotations

import json
import sys

import yaml

prov = json.load(open(sys.argv[1], encoding="utf-8"))
bundle = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
records = prov["bundles"]
assert len(records) == 1 and records[0]["id"] == bundle["id"], records
assert records[0]["version"] == bundle["version"], records
assert records[0]["content_digest"] == bundle["content_digest"], records
print("provenance records the atlas bundle", records[0]["id"], records[0]["version"])
