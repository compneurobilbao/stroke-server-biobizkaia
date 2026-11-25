#!/bin/sh
# SHA-256 that pins the part of an author's repository a wrapper uses (card STEPS-01): the hash
# of the sorted lines "<sha256>  <path>" (the output of sha256sum) of every file under the given
# paths, relative to DIR. Independent of how the archive was compressed. Used by the
# Dockerfiles and by steps/fetch-upstream.sh, against UPSTREAM_SHA256 of the step's upstream.env.
#   steps/upstream-digest.sh DIR PATH...
set -eu
dir="$1"
shift
cd "$dir"
# the sed only normalises the binary-mode marker some platforms print ("<hash> *<path>")
find "$@" -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum \
  | sed 's/^\([0-9a-f]\{64\}\) \*/\1  /' | sha256sum | cut -d ' ' -f 1
