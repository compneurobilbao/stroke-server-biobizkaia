#!/usr/bin/env bash
# Fetch the contract files, the step SDK and the synthetic fixture cases of the public
# stroke-server repository at the pinned commit (step/ci/STROKE_SERVER_SHA) into DEST.
# Nothing from there is committed to this repository.
#   step/ci/fetch_pinned.sh DEST
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sha="$(tr -d '[:space:]' < "$here/STROKE_SERVER_SHA")"
dest="${1:?usage: fetch_pinned.sh DEST}"
mkdir -p "$dest"
curl -fsSL "https://codeload.github.com/compneurobilbao/stroke-server-biobizkaia/tar.gz/${sha}" \
  | tar -xz -C "$dest" --strip-components=1 --wildcards '*/contracts' '*/steps/sdk' '*/fixtures'
test -f "$dest/contracts/validate.py" && test -f "$dest/steps/sdk/pyproject.toml"
echo "fetched stroke-server ${sha} into ${dest}"
