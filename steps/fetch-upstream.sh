#!/usr/bin/env bash
# Fetch the author's repository a wrapper step consumes, at the commit pinned in the step's
# upstream.env, into DEST (CI and local tests; the Dockerfiles download the same archive), and
# check the content of the files the wrapper uses (UPSTREAM_PATHS) against UPSTREAM_SHA256
# (steps/upstream-digest.sh); a mismatch fails. Nothing is committed. DEST/UPSTREAM_COMMIT
# records the commit.
#   steps/fetch-upstream.sh steps/<step> DEST
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
step="${1:?usage: fetch-upstream.sh STEP_DIR DEST}"
dest="${2:?usage: fetch-upstream.sh STEP_DIR DEST}"
# shellcheck disable=SC1091
. "$step/upstream.env"
: "${UPSTREAM_REPOSITORY:?upstream.env has no UPSTREAM_REPOSITORY}"
: "${UPSTREAM_COMMIT:?upstream.env has no UPSTREAM_COMMIT}"
: "${UPSTREAM_PATHS:?upstream.env has no UPSTREAM_PATHS}"
: "${UPSTREAM_SHA256:?upstream.env has no UPSTREAM_SHA256}"
[[ "$UPSTREAM_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "UPSTREAM_COMMIT is not a 40-hex commit" >&2; exit 1; }
mkdir -p "$dest"
curl -fsSL "https://codeload.github.com/${UPSTREAM_REPOSITORY}/tar.gz/${UPSTREAM_COMMIT}" \
  | tar -xz -C "$dest" --strip-components=1
# shellcheck disable=SC2086
digest="$(sh "$here/upstream-digest.sh" "$dest" $UPSTREAM_PATHS)"
if [ "$digest" != "$UPSTREAM_SHA256" ]; then
  echo "content of ${UPSTREAM_PATHS} at ${UPSTREAM_COMMIT}: sha256 ${digest}, expected ${UPSTREAM_SHA256}" >&2
  exit 1
fi
printf '%s\n' "$UPSTREAM_COMMIT" > "$dest/UPSTREAM_COMMIT"
echo "fetched ${UPSTREAM_REPOSITORY} ${UPSTREAM_COMMIT} into ${dest} (content ${UPSTREAM_SHA256})"
