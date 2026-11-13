# lesion-qc

Pipeline slot `lesion-intake`: validates a lesion mask and measures it.

| Mode | Inputs present | Behaviour |
|---|---|---|
| MNI mask | `lesion_mni` (`lesion_mask_mni_any`) | Checks header space and extent, resamples to the 2 mm working grid (linear, threshold 0.5), writes `lesion_mask_mni`. |
| CT | `lesion_native` (+ optional `ct`) | QC on the native mask, no resampling, no `lesion_mask_mni`; hemisphere is `not_assessed`. `ct` is only the figure background and must share the mask grid. |

Outputs: `lesion_qc` (JSON, schema in [`lesion_qc.schema.json`](lesion_qc.schema.json)), `qc_figure` (PNG), `lesion_mask_mni` (MNI mode only).

Rejections (exit 10, declared codes in `step.yaml`): format and non-binary values (SDK), `mask_empty`, `mask_not_3d`, `affine_invalid`, `space_not_declared`, `space_mismatch`, `mask_empty_after_resampling`, `ct_grid_mismatch`, `conflicting_inputs`, `lesion_input_missing`, `lesion_too_large`, `bilateral_lesion` (only with `reject_bilateral`). Warnings (in `lesion_qc` and `provenance.json`): `small_lesion`, `multiple_components`, `bilateral_lesion`, `volume_changed_by_resampling`, `space_code_not_mni152`.

Local check: `python -m pytest steps/lesion-qc` (needs `steps/sdk/requirements-image.txt` and `requirements-step.txt`).
