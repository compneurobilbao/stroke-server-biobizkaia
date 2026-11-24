#!/usr/bin/env bash
# Download the public High Resolution CT Template files used to build the reference case, at a
# pinned commit, and verify SHA-256. Nothing is committed to this repository.
#   Source : https://github.com/muschellij2/high_res_ct_template (Muschelli 2020,
#            https://doi.org/10.1007/978-3-030-50153-2_27)
#   Licence: CC BY-NC-SA 4.0 (the template is derived from CQ500 scans, CC BY-NC-SA 4.0;
#            http://headctstudy.qure.ai/dataset), de-identified research data, used here only
#            for non-commercial test purposes.
#   step/ci/fetch_reference_data.sh DEST
set -euo pipefail
dest="${1:?usage: fetch_reference_data.sh DEST}"
sha=135392e7782eb87b923c054ada1c50f97034e431
base="https://raw.githubusercontent.com/muschellij2/high_res_ct_template/${sha}/template"
mkdir -p "$dest"
fetch() { # path-in-repo local-name sha256
  local f="$dest/$2"
  if [ -f "$f" ] && echo "$3  $f" | sha256sum -c - >/dev/null 2>&1; then echo "cached: $2"; return; fi
  curl -fsSL -o "$f" "$base/$1"
  echo "$3  $f" | sha256sum -c -
}
fetch 1mm/template_with_skull_1mm.nii.gz template_with_skull_1mm.nii.gz 74f3bc16b7c05b2fb8a67803367a979af811f5d40189d699a4935a960c5a3e02
fetch 1mm/template_1mm.nii.gz template_1mm.nii.gz cc622135b56dfa0b1f8f528188775dec4532931e1b54b344c9d3be59ef5b55a8
fetch Template_to_MNIcomptx.nii.gz Template_to_MNIcomptx.nii.gz bf5d8ed8ca4738847bf53068669fe1b36e813f9873d1df33d502b335679e7837
