#!/usr/bin/env bash
# Atlas-space resampling (step 1.1.0) under Docker: the toy atlas that declares ToyAtlasSpace,
# with the identity and the 4 mm shift space-transform bundles, on the "main" and "small"
# lesions. Tables go to OUT/<transform>_<case>.tsv (compare with compare_oracle.py against
# REFERENCE_DIR/space/expected). Then the two refusals: no transform bundle mounted
# (space_transform_missing, exit 1) and a transform to another space (space_transform_mismatch).
# Contract section 6 flags, as the Docker executor uses.
#   steps/lnm-tracts/ci/run_space_cases.sh IMAGE REFERENCE_DIR OUT
set -euo pipefail
image="${1:?usage: run_space_cases.sh IMAGE REFERENCE_DIR OUT}"
ref="$(cd "${2:?}" && pwd)"
out="${3:?}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

# run_one NAME LESION TRANSFORM_DIR_OR_EMPTY -> exit code of the step
run_one() {
  local name="$1" lesion="$2" transform="$3" work="$out/$1"
  rm -rf "$work"
  mkdir -p "$work/in" "$work/out"
  chmod 777 "$work/out"
  cp "$lesion" "$work/in/lesion_mask.nii.gz"
  echo '{}' > "$work/in/params.json"
  local mounts=(-v "$ref/space/atlas:/ref/tract_atlas:ro")
  if [ -n "$transform" ]; then mounts+=(-v "$transform:/ref/space_transform:ro"); fi
  set +e
  docker run --rm --read-only --user 12345:12345 --cap-drop ALL \
    --security-opt no-new-privileges --network none --tmpfs /tmp:rw,mode=1777,size=2g \
    -v "$work/in:/in:ro" -v "$work/out:/out" "${mounts[@]}" \
    -e HOME=/tmp -e TMPDIR=/tmp -e TZ=UTC -e STEP_CPUS=2 -e OMP_NUM_THREADS=2 \
    --entrypoint /usr/local/bin/run "$image" \
    --in /in --out /out --params /in/params.json > "$work/log.txt" 2>&1
  local code=$?
  set -e
  return "$code"
}

for transform in identity shift; do
  for case in main small; do
    name="${transform}_${case}"
    if run_one "$name" "$ref/cases/$case.nii.gz" "$ref/space/$transform"; then
      cp "$out/$name/out/tract_disconnection.tsv" "$out/$name.tsv"
      grep -o '"lesion_resampled_to_atlas_space"[^}]*' "$out/$name/out/provenance.json" | head -1
      test -f "$out/$name/out/extra/lesion_mask_atlas_space.nii.gz"
      echo "case $name: exit 0"
    else
      echo "case $name: FAILED"; cat "$out/$name/log.txt"; exit 1
    fi
  done
done

expect_failure() {
  local name="$1" transform="$2" code_expected="$3" code=0
  # run_one restores errexit before it returns, so its status is taken with ||
  run_one "$name" "$ref/cases/main.nii.gz" "$transform" || code=$?
  echo "case $name: exit $code"
  cat "$out/$name/out/error.json"; echo
  test "$code" -eq 1
  grep -q "\"code\": *\"$code_expected\"" "$out/$name/out/error.json"
}
expect_failure no_transform "" space_transform_missing
expect_failure other_space "$ref/space/mismatch" space_transform_mismatch
echo "atlas-space cases done"
