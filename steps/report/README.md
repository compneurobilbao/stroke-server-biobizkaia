# research-report

Pipeline slot `report`: builds the research report of a run from the outputs of the previous steps.

| Input | Type | Required | If absent |
|---|---|---|---|
| `lesion_qc` | `lesion_qc` | yes | run rejected (`input_missing`) |
| `lesion_mask` | `lesion_mask_mni` | yes | run rejected |
| `run_provenance` | `run_provenance` | yes | run rejected |
| `ct_mni` | `ct_mni` | no | figures use the MNI152 template bundled with nilearn |
| `fdisconnection_map` | `fdisconnection_map` | no | the section says the input was not provided |
| `tract_disconnection` | `tract_disconnection` (TSV: `tract`, `disconnection`) | no | same |
| `cognet_profile` | `cognet_profile` (validated against `contracts/schemas/v1/cognet-profile.schema.json`) | no | same |

Parameter `language`: `en` (default) or `es`. Outputs: `report_pdf` and `report_json`.

## Outputs

- **PDF** (Jinja2 templates in `src/research_report_step/templates/`, rendered by WeasyPrint with `data:` images only, no network): research-use banner in the running header of every page ("for research use only, not for clinical decisions"), summary, lesion section with an orthogonal-view figure (nilearn `plot_roi`), functional disconnection map (nilearn `plot_stat_map`), tract disconnection table and bar chart, COGNET domain table, and a provenance section with the image reference and digest of every slot of the run, the reference bundles and the SHA-256 of every artefact the report read. English and Spanish are separate templates; Spanish text lives only there.
- **`report.json`** (schema: [`report.schema.json`](report.schema.json)): the canonical machine-readable content, language-neutral except `language`. Canonical form: UTF-8, sorted keys, no whitespace, no trailing newline, floats in fixed notation rounded to 6 decimals, no clock values of its own, no software versions, no accounting block of the run provenance. Equal inputs give identical bytes; the file is the basis for later sealing.

## Tests

`python -m pytest steps/report` (needs `steps/sdk/requirements-image.txt` and `requirements-step.txt`; the PDF tests also need Pango, HarfBuzz and DejaVu fonts, and `pypdf`, and are skipped when WeasyPrint cannot load).

- `tests/golden/report_full_en.json` and `report_min_es.json`: `report.json` compared byte for byte.
- `tests/golden/*.txt`: expected PDF text. PDF bytes are never compared; the text of every page is extracted with pypdf, banner and page counter are removed, whitespace is ignored (`tests/report_pdftext.py`). Banner presence is asserted page by page.
- Regenerate goldens after an intended change: `REPORT_UPDATE_GOLDEN=1 python -m pytest steps/report`. `REPORT_ACTUAL_DIR=<dir>` keeps the generated PDFs and texts (CI uploads them).
- Inputs are synthetic: a mask from `fixtures/data/mni_masks`, values invented in `tests/make_reference.py`, and a run provenance with fake digests in `tests/data/run_provenance.json`.

## Image

Base `python:3.12.8-slim-bookworm`, Debian packages `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz-subset0`, `fontconfig`, `fonts-dejavu-core` (not version-pinned: Debian point releases), all Python dependencies pinned in `requirements-step.txt`. The image is larger than the other steps because nilearn pulls in pandas and scikit-learn; CI prints the size.
