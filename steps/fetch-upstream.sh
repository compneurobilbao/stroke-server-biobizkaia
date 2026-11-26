#!/usr/bin/env bash
# Fetch the author's repository a wrapper step consumes, at the commit pinned in the step's
# upstream.env, into DEST (CI and local tests; the Dockerfiles download the same archive), and
# check the content of the files the wrapper uses (UPSTREAM_PATHS) against UPSTREAM_SHA256
# (steps/upstream-digest.sh); a mismatch fails. Nothing is committed. DEST/UPSTREAM_COMMIT
# records the commit.
#   steps/fetch-upstream.sh steps/<step> DEST
# A private repository (an author's repository before its first public release) is read with
# the token in UPSTREAM_TOKEN (or GITHUB_TOKEN) through the GitHub API when the anonymous
# download fails. Exit code 3 means that the repository itself is not visible with the
# available credentials (nothing fetched); any other failure (unknown commit, network, content
# mismatch) exits 1, so that CI can tell "no access" from a broken pin.
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
token="${UPSTREAM_TOKEN:-${GITHUB_TOKEN:-}}"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
if ! curl -fsSL -o "$archive" "https://codeload.github.com/${UPSTREAM_REPOSITORY}/tar.gz/${UPSTREAM_COMMIT}" 2>/dev/null; then
  fetched=false
  auth=()
  [ -n "$token" ] && auth=(-H "Authorization: Bearer ${token}")
  if [ -n "$token" ] && curl -fsSL -o "$archive" "${auth[@]}" \
      "https://api.github.com/repos/${UPSTREAM_REPOSITORY}/tarball/${UPSTREAM_COMMIT}"; then
    fetched=true
  fi
  if [ "$fetched" != true ]; then
    status="$(curl -sS -o /dev/null -w '%{http_code}' ${auth[@]+"${auth[@]}"} \
      "https://api.github.com/repos/${UPSTREAM_REPOSITORY}" || true)"
    if [ "$status" = 404 ] || [ "$status" = 401 ] || [ "$status" = 403 ]; then
      echo "${UPSTREAM_REPOSITORY} is not readable with the available credentials (HTTP ${status});" \
        "a private repository needs UPSTREAM_TOKEN (or GITHUB_TOKEN) with read access to it" >&2
      exit 3
    fi
    echo "could not download ${UPSTREAM_REPOSITORY} at ${UPSTREAM_COMMIT} (repository visible, HTTP ${status})" >&2
    exit 1
  fi
fi
mkdir -p "$dest"
tar -xzf "$archive" -C "$dest" --strip-components=1
# shellcheck disable=SC2086
digest="$(sh "$here/upstream-digest.sh" "$dest" $UPSTREAM_PATHS)"
if [ "$digest" != "$UPSTREAM_SHA256" ]; then
  echo "content of ${UPSTREAM_PATHS} at ${UPSTREAM_COMMIT}: sha256 ${digest}, expected ${UPSTREAM_SHA256}" >&2
  exit 1
fi
printf '%s\n' "$UPSTREAM_COMMIT" > "$dest/UPSTREAM_COMMIT"
echo "fetched ${UPSTREAM_REPOSITORY} ${UPSTREAM_COMMIT} into ${dest} (content ${UPSTREAM_SHA256})"
