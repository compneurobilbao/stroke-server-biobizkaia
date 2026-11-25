#!/usr/bin/env bash
# CI only: build the CT reference case of the normalisation step (ct-lesion-to-mni152) from a
# real, public head CT template, using the scripts of the wrapper (steps/ct-lesion-to-mni152/ci)
# and the template of the author's package at the commit pinned in registry.yaml (fetched by
# `registry.py fetch` into EXTERNAL; only an optional sanity check reads it). Nothing is committed.
#
#   ct_reference.sh EXTERNAL REFDATA OUT
#     EXTERNAL  directory written by `registry.py fetch` (contains CTLesion2MNI152/)
#     REFDATA   download directory of the template files (cache it between runs)
#     OUT       receives ct.nii.gz, lesion_mask_native.nii.gz, lesion_mask_mni.nii.gz (ground
#               truth on the 2 mm working grid) and source_info.json
#
# Data: High Resolution CT Template (2020), CC BY-NC-SA 4.0, derived from the public CQ500 scans
# (de-identified research data). It is downloaded by the wrapper's fetch script at a pinned commit
# and verified by SHA-256; it is used here for non-commercial test purposes only and is never
# part of an image, a bundle or a repository. The case is synthetic: the template warped to the
# MNI grid, a lesion inserted in MNI space, then a known random affine and a smooth deformation.
# Needs numpy, nibabel and scipy in the active Python; the synthetic fixture generator of this
# repository (fixtures/) supplies the transform code.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
external="${1:?usage: ct_reference.sh EXTERNAL REFDATA OUT}"
refdata="${2:?usage: ct_reference.sh EXTERNAL REFDATA OUT}"
out="${3:?usage: ct_reference.sh EXTERNAL REFDATA OUT}"
step="$repo/steps/ct-lesion-to-mni152"
package="$external/CTLesion2MNI152"
test -f "$package/src/ct_lesion_to_mni152/data/ct_in_MNI152.nii.gz"   || { echo "CTLesion2MNI152 sources not found in $package (registry.py fetch)" >&2; exit 1; }
bash "$step/ci/fetch_reference_data.sh" "$refdata"
# optional sanity check of the generator against the package template
cp "$package/src/ct_lesion_to_mni152/data/ct_in_MNI152.nii.gz" "$refdata/"
python "$step/ci/make_reference.py" "$refdata" "$repo" "$out"
