#!/usr/bin/env bash
# Build the reference bundle xfm-nlin6asym-to-nlin2009aasym 1.0.0 (type space-transform):
# the TemplateFlow MNI152NLin6Asym -> MNI152NLin2009cAsym transform as a coordinate map on the
# MNI152NLin2009aAsym 1 mm grid (docs/reference-data.md, "The space-transform bundle").
#
#   scripts/bundles/space_transform/build.sh WORK_DIR
#
# Every source is downloaded at a pinned URL and checked against its SHA-256; the bundle is
# written to WORK_DIR/bundle/xfm-nlin6asym-to-nlin2009aasym/1.0.0 (then validate and register it
# as any bundle). Nothing is committed: the bundle (about 100 MB) lives in the bundle root.
#
# Needs: curl, sha256sum, a Python with nibabel, numpy and PyYAML (PYTHON, default python3), and
# ANTs antsApplyTransforms, either on PATH / in ANTSPATH or inside a Docker image given as
# ANTS_IMAGE (the binary is looked up on the image's PATH, or set ANTS_BIN to its full path in
# the image; ANTS_VERSION names the release for the record; it is written to the bundle's
# metadata.built_with). Downloads about 250 MB; peak memory about 3 GB.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="${1:?usage: build.sh WORK_DIR}"
mkdir -p "$work"
work="$(cd "$work" && pwd)"
PYTHON="${PYTHON:-python3}"
TF=https://templateflow.s3.amazonaws.com
out="$work/bundle/xfm-nlin6asym-to-nlin2009aasym/1.0.0"
[ ! -e "$out" ] || { echo "$out exists; bundle versions are immutable" >&2; exit 1; }

# path under $TF  SHA-256 (same list as make_bundle.py)
sources=(
  "tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_from-MNI152NLin6Asym_mode-image_xfm.h5 2e3869a07b96aec406e0419ca2e434afc54882d37cc212b933b139d1b63a4dfe"
  "tpl-MNI152NLin2009cAsym/LICENSE 46e280fe1a24c6f37feef2445ed288435e0ad063504a06030b206b12fdd2629d"
  "tpl-MNI152NLin2009aAsym/tpl-MNI152NLin2009aAsym_res-1_T1w.nii.gz a12fa7be3c02762e14ec770e940a46ce57dc6d7e4953c5228e4da646301b9fe1"
  "tpl-MNI152NLin2009aAsym/tpl-MNI152NLin2009aAsym_res-1_desc-brain_mask.nii.gz 17afe7c0696e788aeceb656e16bb5e907e8d22cf8871e088e31b31c9a1941b67"
  "tpl-MNI152NLin6Asym/tpl-MNI152NLin6Asym_res-01_T1w.nii.gz f675946c0a077e0682885c3d6085f10acb0dd22b2274a6cbbf04969c08a77258"
)
for entry in "${sources[@]}"; do
  rel="${entry% *}"; sum="${entry#* }"
  dest="$work/dl/$rel"
  if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum -c --status -; then
    mkdir -p "$(dirname "$dest")"
    curl -fsSL --retry 3 -o "$dest.part" "$TF/$rel"
    mv "$dest.part" "$dest"
  fi
  echo "$sum  $dest" | sha256sum -c -
done

xfm=dl/tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_from-MNI152NLin6Asym_mode-image_xfm.h5
ref=dl/tpl-MNI152NLin2009aAsym/tpl-MNI152NLin2009aAsym_res-1_T1w.nii.gz
src=dl/tpl-MNI152NLin6Asym/tpl-MNI152NLin6Asym_res-01_T1w.nii.gz
mkdir -p "$work/ants"

# Run antsApplyTransforms with paths relative to WORK_DIR (mounted at /w in the Docker case).
ants() {
  if [ -n "${ANTS_IMAGE:-}" ]; then
    docker run --rm --user "$(id -u):$(id -g)" -v "$work:/w" -w /w --entrypoint "${ANTS_BIN:-antsApplyTransforms}" \
      "$ANTS_IMAGE" "$@"
  else
    (cd "$work" && "${ANTSPATH:+${ANTSPATH%/}/}antsApplyTransforms" "$@")
  fi
}
# antsApplyTransforms of some releases does not print a version: set ANTS_VERSION then.
ants_version="${ANTS_VERSION:-$(ants --version 2>/dev/null | head -1 | sed 's/^ANTs Version: *//' || true)}"
[ -n "$ants_version" ] || ants_version="ANTs (version not reported)"
echo "antsApplyTransforms: $ants_version"

# 1. the transform composed into one displacement field on the 2009a grid (ITK, LPS vectors)
ants -d 3 -r "$ref" -t "$xfm" -o "[ants/displacement.nii.gz,1]" -v 0
# 2. the NLin6Asym template resampled by ANTs, to check the conversion in make_bundle.py
ants -d 3 -i "$src" -r "$ref" -t "$xfm" -n Linear -o ants/nlin6asym_T1w_in_2009a.nii.gz -v 0

"$PYTHON" "$here/make_bundle.py" --work "$work" --out "$out" --ants-version "$ants_version"
echo "bundle written to $out"
