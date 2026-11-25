"""Compare the profile of this image with that of the previously published image (1.0.0).

    python steps/cognet-stroke/ci/compare_previous.py NEW_PROFILE PREVIOUS_PROFILE

On a disconnection map without NaN, 1.1.0 (the author's compute_cdr) must give exactly the
profile of 1.0.0 (its own implementation): every score, p-value, correction, flag and
significance, the method and the bundle record. Only profile_version differs (1.0 -> 1.1).
"""

from __future__ import annotations

import json
import sys

new = json.load(open(sys.argv[1], encoding="utf-8"))
old = json.load(open(sys.argv[2], encoding="utf-8"))
assert (old["profile_version"], new["profile_version"]) == ("1.0", "1.1"), (
    old["profile_version"], new["profile_version"])
for doc in (new, old):
    doc.pop("profile_version")
if new != old:
    for key in sorted(set(new) | set(old)):
        if new.get(key) != old.get(key):
            print(f"differs: {key}")
    sys.exit(1)
n = len(new["concepts"])
print(f"profiles identical apart from profile_version: {n} concepts, {len(new['domains'])} domains")
