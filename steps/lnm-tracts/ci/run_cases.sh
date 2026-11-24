#!/usr/bin/env bash
# Run the step image on every synthetic lesion of the reference dataset, all at once (the
# containers share one read-only atlas mount, which checks that cases do not interfere), and
# collect the tables as OUT/<case>.tsv. Contract section 6 flags, as the Docker executor uses.
#   step/ci/run_cases.sh IMAGE REFERENCE_DIR OUT
set -euo pipefail
image="${1:?usage: run_cases.sh IMAGE REFERENCE_DIR OUT}"
ref="$(cd "${2:?}" && pwd)"
out="${3:?}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

fingerprint() { (cd "$ref/bundle" && find . -type f | LC_ALL=C sort | xargs sha256sum | sha256sum); }
before="$(fingerprint)"

cases=()
for f in "$ref"/cases/*.nii.gz; do
  name="$(basename "$f" .nii.gz)"
  cases+=("$name")
  work="$out/$name"
  rm -rf "$work"
  mkdir -p "$work/in" "$work/out"
  chmod 777 "$work/out"
  cp "$f" "$work/in/lesion_mask.nii.gz"
  echo '{}' > "$work/in/params.json"
done

pids=()
for name in "${cases[@]}"; do
  work="$out/$name"
  docker run --rm --read-only --user 12345:12345 --cap-drop ALL \
    --security-opt no-new-privileges --network none --tmpfs /tmp:rw,mode=1777,size=2g \
    -v "$work/in:/in:ro" -v "$work/out:/out" -v "$ref/bundle:/ref/tract_atlas:ro" \
    -e HOME=/tmp -e TMPDIR=/tmp -e TZ=UTC -e STEP_CPUS=2 -e OMP_NUM_THREADS=2 \
    --entrypoint /usr/local/bin/run "$image" \
    --in /in --out /out --params /in/params.json > "$work/log.txt" 2>&1 &
  pids+=($!)
done

status=0
for i in "${!cases[@]}"; do
  if wait "${pids[$i]}"; then
    echo "case ${cases[$i]}: exit 0"
  else
    echo "case ${cases[$i]}: FAILED"; cat "$out/${cases[$i]}/log.txt"; status=1
  fi
done
[ "$status" -eq 0 ]

for name in "${cases[@]}"; do
  cp "$out/$name/out/tract_disconnection.tsv" "$out/$name.tsv"
done
[ "$before" = "$(fingerprint)" ] && echo "atlas bundle unchanged"
