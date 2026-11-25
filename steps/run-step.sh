#!/usr/bin/env bash
# Run a step image once with the flags of contract section 6 (as the docker executor does):
# read-only root, arbitrary UID, no capabilities, no network, /tmp as tmpfs. CI helper.
#   steps/run-step.sh IMAGE IN_DIR OUT_DIR [NAME=BUNDLE_DIR ...]
# IN_DIR holds the staged inputs and params.json; OUT_DIR is created (must not exist or be
# empty). Each NAME=BUNDLE_DIR is mounted read-only at /ref/NAME. Prints the exit code and
# exits with it.
set -uo pipefail
image="${1:?usage: run-step.sh IMAGE IN_DIR OUT_DIR [NAME=BUNDLE_DIR ...]}"
in_dir="$(cd "${2:?}" && pwd)"
out_dir="${3:?}"
shift 3
mkdir -p "$out_dir"
chmod 777 "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
mounts=()
for spec in "$@"; do
  name="${spec%%=*}"
  dir="$(cd "${spec#*=}" && pwd)"
  mounts+=(-v "$dir:/ref/$name:ro")
done
docker run --rm --read-only --user 12345:12345 --cap-drop ALL \
  --security-opt no-new-privileges --network none --tmpfs /tmp:rw,mode=1777,size=2g \
  -v "$in_dir:/in:ro" -v "$out_dir:/out" "${mounts[@]}" \
  -e HOME=/tmp -e TMPDIR=/tmp -e TZ=UTC -e STEP_CPUS=2 \
  --entrypoint /usr/local/bin/run "$image" --in /in --out /out --params /in/params.json
code=$?
echo "exit code: $code"
exit "$code"
