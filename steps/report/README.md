# research-report

Pipeline slot `report`: builds the research report of a run from the outputs of the previous steps.

| Input | Type | Required | If absent |
|---|---|---|---|
| `lesion_qc` | `lesion_qc` | yes | run rejected (`input_missing`) |
| `lesion_mask` | `lesion_mask_mni` | yes | run rejected |
| `run_provenance` | `run_provenance` (validated against the 1.2 schemas copied into the image; records of contract 1.0 to 1.2) | yes | run rejected |
| `ct_mni` | `ct_mni` | no | figures use the MNI152 template bundled with nilearn |
| `fdisconnection_map` | `fdisconnection_map` | no | the section says the input was not provided |
| `tract_disconnection` | `tract_disconnection` (TSV: `tract`, `disconnection`) | no | the section says tract disconnection is not part of this analysis (QC-only report, version 0.2.0); `report.json` has `{status: absent}` as for the other optional sections |
| `cognet_profile` | `cognet_profile` (validated against `contracts/schemas/v1/cognet-profile.schema.json`) | no | same |

Parameter `language`: `en` (default) or `es`. Outputs: `report_pdf` and `report_json`.

## Contract 1.2: plan, external tools, declared origins (version 0.3.0)

The step declares contract 1.2 and reads the run record unchanged, so presets and custom plans that include the report run. From the record it states:

- **The plan** (`plan`): full analysis, preset (id, version, digest of the preset file) or custom targets, the required and if-available targets, and the role of every slot (`target`, `upstream`, `if_available`, or not requested / not applicable).
- **Slots not requested**: an analysis whose slot is outside the plan (`reason: not_requested`) has status `not_requested` in `report.json` and "not requested in this plan" in the PDF, not "input not provided".
- **External tools** (`external_tools`, PDF section 5.4): per selected slot whose implementation carries an `upstream` block, the wrapper step and the upstream name, version, homepage, licence and citation. The run record carries no validation level of an implementation, so the report states none and says so; it does not infer "experimental" from anything else. Stub implementations keep the "(stub)" mark.
- **Declared origins** (`declared_origins`, PDF section 5.5): inputs whose uploader declared an origin, shown as "declared by the user" (the platform cannot verify them).

These keys and the sections 5.4 and 5.5 exist only when the record has the information. The report of a run with a 1.0 or 1.1 record has the content of the previous version apart from the version fields (`report_version` 1.1, `generator.version`).

## Version 0.4.0: parcels without a value

- A functional disconnection map with NaN voxels (parcels whose statistic is undefined, as `flnm-fast` writes them) is accepted: `max`, `n_positive_voxels` and `mean_of_positive` use the voxels that have a value, and `n_undefined_voxels` (only when above zero) is recorded and shown in section 3.1. Infinite values and a map with no value at all are still rejected (`input_format_invalid`); the figure draws undefined voxels as zero.
- When the COGNET profile has `excluded_parcels` with `count` above zero, `report.json` copies it to `cognitive_profile.excluded_parcels`, adds the note `parcels_excluded_from_cognitive_profile`, and section 4 states "N parcels (x %) had no defined value in the disconnection map and were excluded from the cognitive profile" (Spanish template: the same in Spanish); section 6 adds a note that the lesion overlap is not affected. A profile without the field, or with `count` 0, gives the content of 0.3.0 apart from the version fields (`report_version` 1.2, `generator.version`).

## Version 0.5.0: synthetic example inputs

- A platform that offers synthetic example lesions stores an attached example with the reserved declared origin `external-tool` / `stroke-server-example`. When any input of the run record carries it, the report adds the note `synthetic_example_input` (`report_version` 1.3), the first page shows the box "Example input: synthetic lesion, not patient data" below the research-use box (Spanish template: the same in Spanish) and section 6 repeats it. No new input or contract field is needed: the origin is read from the run record (contract 1.2, `inputs[].origin`), so a record without it gives the content of 0.4.0 apart from the version fields (`report_version`, `generator.version`).

## Outputs

- **PDF** (Jinja2 templates in `src/research_report_step/templates/`, rendered by WeasyPrint with `data:` images only, no network): research-use banner in the running header of every page ("for research use only, not for clinical decisions"), summary, lesion section with an orthogonal-view figure (nilearn `plot_roi`), functional disconnection map (nilearn `plot_stat_map`), tract disconnection table and bar chart, COGNET domain table, and a provenance section with the image reference and digest of every slot of the run, the reference bundles and the SHA-256 of every artefact the report read. English and Spanish are separate templates; Spanish text lives only there.
- **`report.json`** (schema: [`report.schema.json`](report.schema.json)): the canonical machine-readable content, language-neutral except `language`. Canonical form: UTF-8, sorted keys, no whitespace, no trailing newline, floats in fixed notation rounded to 6 decimals, no clock values of its own, no software versions, no accounting block of the run provenance. Equal inputs give identical bytes; the file is the basis for later sealing.

## Tests

`python -m pytest steps/report` (needs `steps/sdk/requirements-image.txt` and `requirements-step.txt`; the PDF tests also need Pango, HarfBuzz and DejaVu fonts, and `pypdf`, and are skipped when WeasyPrint cannot load).

- `tests/golden/report_full_en.json`, `report_notract_en.json` (everything except the tract table), `report_min_es.json` (these three use the 1.0 run record) and, with the 1.2 records in `tests/data/run_provenance_{preset,custom,external}.json`, `report_preset_en.json` (preset `mni-mask-analysis`), `report_custom_es.json` (free plan, three slots not requested) and `report_external_en.json` (external tool for the functional slot and a declared mask origin): `report.json` compared byte for byte.
- `tests/golden/*.txt`: expected PDF text. PDF bytes are never compared; the text of every page is extracted with pypdf, banner and page counter are removed, whitespace is ignored (`tests/report_pdftext.py`). Banner presence is asserted page by page.
- Regenerate goldens after an intended change: `REPORT_UPDATE_GOLDEN=1 python -m pytest steps/report`. `REPORT_ACTUAL_DIR=<dir>` keeps the generated PDFs and texts (CI uploads them).
- Inputs are synthetic: a mask from `fixtures/data/mni_masks`, values invented in `tests/make_reference.py`, and a run provenance with fake digests in `tests/data/run_provenance.json`.

## Image

Base `python:3.12.8-slim-bookworm`, Debian packages `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz-subset0`, `fontconfig`, `fonts-dejavu-core` (not version-pinned: Debian point releases), all Python dependencies pinned in `requirements-step.txt`. The image is larger than the other steps because nilearn pulls in pandas and scikit-learn; CI prints the size.
