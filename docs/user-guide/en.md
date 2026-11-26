# User guide for lab researchers

Spanish version: [es.md](es.md).

This guide explains how to run the minimum analysis pipeline from the web platform. It is written for researchers, not developers. Names in **bold** are the labels you see on screen (English interface).

The platform is for **research use only**. Its results are not for clinical decisions, and every PDF page says so.

## 1. What the platform does today

Open **About the process** in the main navigation to see the overview ("How a lesion becomes a research report"). Today the platform starts from a binary lesion mask that is already in standard space (MNI152NLin6Asym, 2 mm) and runs two analyses on it:

- **Functional disconnection**: functional lesion network mapping (flnm-fast) against a normative functional connectome gives a disconnection map; the COGNET cognitive profile is estimated from that map.
- **Structural disconnection**: tract-wise disconnection (lnm-tracts) with the HCP1065 white-matter tracts gives a tract table.

Both results are gathered into a **Research report** (PDF) and an interactive viewer.

The overview also shows greyed-out stages marked **In development**: **Normalisation to MNI**, **Lesion segmentation**, **Lesion bank** and **Voxel-wise structural LNM**. They are not available yet and cannot be selected.

## 2. Sign in

1. Open the platform address your administrator gave you.
2. Enter your **Email** and **Password** and press **Sign in**.
3. Use **Sign out** when you finish. If you see "The session has expired. Please sign in again.", sign in again.

Accounts are created by an administrator; there is no public registration. The language can be switched with the **Language** selector (English or Spanish).

## 3. Prepare your lesion mask

You need one file: a lesion mask in `.nii.gz` format that is already registered to MNI152NLin6Asym. The platform does not normalise masks for you today.

The upload is checked in two stages.

**On upload** (the file is refused immediately):

- The file name must end in `.nii.gz`, the file must not be empty, and it must be a gzip-compressed NIfTI image.
- It must not exceed the maximum upload size of the deployment (1024 MB unless the administrator changed it).

**Lesion QC step** (the first step of every run; the run stops if the mask fails):

- Binary: only the values 0 and 1.
- A single 3D volume (a single trailing volume is accepted) with a valid voxel-to-world matrix.
- Not empty, and not empty after resampling to the 2 mm working grid (very small lesions can disappear).
- The header must declare a coordinate system, must not declare native space, the axes must not be oblique, and the lesion must lie inside the MNI152 extent.
- Volume not above the maximum (800 mL by default).

The QC step then resamples the mask to the 2 mm grid and records its volume, hemisphere, connected components and bounding box. It also records warnings that do not stop the run, for example a small lesion (below 0.5 mL by default), several components, a lesion in both hemispheres, or a volume that changes after resampling.

Do not put names or identifiers in files or in the case data. File names are not stored.

## 4. Create a case

A case holds your input file and its runs. Go to **Cases** and press **New case**. The wizard has four steps (**Wizard progress**): **Mode and case data**, **Files**, **Implementations** and **Analysis**.

1. **Mode and case data**. Under **Input mode** choose **Lesion mask already in MNI**. The other mode, **CT and lesion mask**, needs the normalisation stage, which is in development, so the lab instance does not run it: the wizard shows it greyed with the tag **In development** and a note ("In development: no implementation of CT lesion to MNI152 is available in this deployment." when the normalisation step is not installed in the deployment). Optionally set **Sex (optional)** and **Age band (optional)**. Only the band is stored; the exact age is never sent to the server. The platform stores only a generated label for the case.
2. **Files**. Select your `.nii.gz` file ("Lesion mask in MNI space (.nii.gz)"). Optionally declare **How was this mask made? (optional)**: **Drawn by hand** or **Created with an external tool** (give **Tool name** and **Tool version**; letters, digits and `. _ + -` only, up to 64 characters). This is shown on the run and in the report as "Declared by the user (not verified)".
3. **Implementations**. Each step uses the default implementation unless you choose another. See section 6 for the cohort.
4. **Analysis**. Choose what to run (section 5).

Finally press **Create case and start run**.

### Try the platform with the synthetic example

If your deployment offers it, the **Files** step shows **Use the example lesion (synthetic)**. Select it instead of uploading a file. It is a synthetic mask, not patient data, and the run and the report mark it as a synthetic example. Press **Remove** to go back to uploading your own file. A case never mixes an example with your own files.

You can start another run on the same case later with **Run again with the same plan** or **Run again with new files**. The declared origin of a case cannot be changed after its first run ("The declared origin cannot be changed after the first run.").

## 5. Choose an analysis (preset or run plan)

In the **Analysis** step the platform lists the analyses it offers. The preselected one carries the tag **Recommended**. An analysis marked **Unavailable** cannot run in this deployment at the moment.

| Option | Meaning |
|---|---|
| A named analysis (preset) | A reviewed, versioned set of outputs. The lab instance offers **Disconnection analysis of an MNI lesion mask**: lesion QC, functional disconnection, COGNET profile, tract-wise disconnection and the research report. All of these outputs are required: if one cannot be produced the run is refused, instead of silently running without it. |
| **Full plan** | Every step that applies to the input mode. |
| **Custom** | You choose the outputs; the steps they need are added automatically. Each output is **Required** (the run is refused if it cannot be produced) or **If available** (produced when possible, skipped otherwise). |

Other presets exist (**Full lesion network analysis**, **Lesion QC and report**, **Normalisation of a CT lesion to MNI152**), but a deployment offers only the ones its administrator has enabled.

**Who can use Custom.** The deployment setting `RUN_PLAN_POLICY` decides:

| Deployment profile | Custom (free mode) |
|---|---|
| `research-public`, `mask-only` | Nobody; presets only. |
| `on-premise` (lab instance) | Administrators and researchers by default (`free_for_researchers`); the administrator can narrow it to `free_for_admins` or `presets_only`. |

If Custom is not allowed for you, the plan panel says "Choosing your own outputs is not allowed for your account in this deployment. Pick one of the analyses."

Below the options, the **Plan** panel shows which steps will run and why: **Requested**, **Added by the plan**, **If available**, **Not part of this plan**. It also says "This plan can run in this deployment." or "This plan cannot run. See the reasons below." with the reasons.

## 6. Choose the normative cohort

The functional disconnection step compares your lesion with a healthy normative cohort. In the **Implementations** step, the functional lesion network mapping step offers:

- **flnm-fast · HCP Aging** (default)
- **flnm-fast · HCP Young Adult**

Pick the cohort that better matches the population you study. The step uses every participant of the chosen cohort: there is no matching by age or sex, so the sex and age band of the case do not affect the result.

Some analyses fix an implementation (**Fixed by this analysis**); you cannot change those. The report step may offer **Research report (English)** and **Research report (Spanish)**; choose according to the PDF language you want.

Next to each implementation the platform shows its validation level and, for unvalidated ones, the tag **experimental** (see section 10). A short note under the list (and the tooltip of the tag) explains the levels.

## 7. What each analysis produces

### Lesion QC

A QC figure and a record (volume, hemisphere, components, bounding box, grid, warnings), plus the lesion mask on the 2 mm working grid.

### Functional disconnection map (flnm-fast)

A map with a signed z score for every parcel of the normative parcellation, written on the 2 mm grid. A positive value means that the parcel is positively coupled with the lesion site. The map is constant within each parcel and zero outside the parcellation. A parcel whose statistic is undefined has no value (NaN). The step rejects a lesion that overlaps no parcel (`lesion_outside_parcellation`).

### Tract table (lnm-tracts)

One row per tract of the HCP1065 atlas with the **disconnection** (the fraction of the tract's streamlines that pass through the lesion, from 0 to 1) and the streamline counts. The tract atlas is defined in ICBM 2009a nonlinear asymmetric space, not in the space of your mask. The step therefore resamples the lesion to the atlas space by nearest-neighbour interpolation before testing the streamlines. The voxel counts and volumes before and after are recorded in the provenance (warning `lesion_resampled_to_atlas_space`). Check them for a small lesion, because resampling can change its volume.

### COGNET cognitive profile

A meta-analytic decoding of the disconnection map. For every concept it gives a score and a Bonferroni-corrected permutation p-value, for three measures: positive disconnection, negative disconnection and lesion overlap. Concepts are grouped into domains. Parcels without a value in the disconnection map are excluded from the disconnection scores and flagged in the profile and in the report (number and fraction of excluded parcels). The lesion overlap does not use that map and is not affected. The step rejects a map with no defined value in any parcel (`disconnection_map_undefined`).

### Research report (PDF)

Every page carries the research-use-only banner. Sections:

1. **Summary**: which analyses are available, not requested, or not part of the analysis; the analysis plan (full plan, preset with its version, or custom outputs); and, when it applies, the line "Example input: synthetic lesion, not patient data".
2. **Lesion**: orthogonal views through the lesion centroid, volume, hemisphere, components, centroid, grid and the warnings recorded by QC. The figure background is an MNI152 template used for orientation only, so small offsets to your mask grid are expected.
3. **Disconnection**: 3.1 the functional map (maximum, number of voxels above zero, mean of the positive voxels, voxels without a value) and 3.2 the tract table (disconnection per tract; the figure shows the most disconnected tracts, up to 20).
4. **Cognitive profile**: method, scores and corrected p-values per domain and measure, and the concepts that are significant after correction. If none is, it says so. It also states how many parcels were left out.
5. **Provenance**: pipeline version and digest, steps with their container image digests, reference data bundles, the artefacts read, external tools, and the origin of inputs as declared by the user.
6. **Notes**: standing notes, for example that parcels were excluded from the cognitive profile, that an input is a synthetic example, or that an origin was declared by the user and is not verified.

A section that was not requested says "Not requested" and stays empty.

## 8. Follow a run and download the results

After you press **Create case and start run**, the **Run** page opens. It shows **Pipeline**, **Input set**, **Created**, **Finished** and the **Step timeline**. Each step shows its state (**Queued**, **Running**, **Succeeded**, **Failed**, **Canceled**, **Skipped**, **Unavailable**), its implementation (with its identifier in brackets when the display name differs, for example "Research report (Spanish) (research-report-es)") and who chose it, each **Attempt**, and a log (**Show log**). Steps outside the plan are listed as "Not part of this plan". The **In development** stages are listed as **Not part of this run**.

To get the outputs:

- **Reports**: **Download PDF report**.
- **Artefacts**: a table with **Output**, **Type** and **Size** and a **Download** button for each file (the functional map, the tract table, the COGNET profile, the QC files and so on). Artefacts that come from the synthetic example carry the tag **synthetic**.
- **View images** opens a read-only viewer with the lesion mask and the disconnection map as layers (**Show**, **Opacity**, **Threshold**). If the run has no CT in MNI space, no anatomical background is shown.
- **Open case** returns to the case.

You can stop a run with **Cancel run**; it stops after the current step.

## 9. When a run fails

If the run cannot start, the wizard shows "The server refused to start this run:" followed by the reasons. The most common ones:

| Message | What to do |
|---|---|
| "No implementation is available in this deployment..." | Ask your administrator to enable the step, or pick an analysis that does not need it. |
| "The analysis ... is not enabled in this deployment." | Pick another analysis. |
| "The analysis ... does not accept the files of this input mode." | Use the other input mode or another analysis. |
| "Choosing your own outputs is not allowed..." | Pick a named analysis. |
| "A reference data bundle needed by ... is not available." | The cohort, atlas or concept data is not installed; ask your administrator. |

If a run fails while running, open the failed step on the **Run** page. The **Failure** field and the step show a short message and the failure **Code** (the same code as in the logs and in the API; the **Failure** field gives the code of the run and that of the failed step, for example `input_rejected / mask_empty`), and **Show log** gives details. The table below lists the codes of the steps. If the cause is in your file, correct it and create a new case, because the files of a case cannot change after its first run. If the cause was temporary, use **Retry from failed step** (or **Resume from canceled step** after a cancel); steps that already succeeded are not repeated.

| Code | Step | Meaning |
|---|---|---|
| `input_missing` | all | A required input was not provided. |
| `input_format_invalid` | all | The file is not a gzip NIfTI image, or the mask has values other than 0 and 1. |
| `invalid_parameters` | all | The parameters do not satisfy the step's schema. |
| `lesion_input_missing` | lesion QC | No lesion mask was provided. |
| `conflicting_inputs` | lesion QC | An MNI mask was provided together with a CT or native mask. |
| `mask_not_3d` | lesion QC | The mask has more than three dimensions. |
| `affine_invalid` | lesion QC | The voxel-to-world matrix is singular or not finite. |
| `mask_empty` | lesion QC, flnm-fast, lnm-tracts, COGNET | The mask has no voxels set. |
| `mask_empty_after_resampling` | lesion QC | The lesion is too small to survive resampling to the 2 mm grid. |
| `space_not_declared` | lesion QC | The header declares no coordinate system, so MNI152 space cannot be confirmed. |
| `space_mismatch` | lesion QC | The header declares native space, the axes are oblique, or the lesion lies outside the MNI152 extent. |
| `ct_grid_mismatch` | lesion QC | The CT and the native mask are not on the same grid. |
| `lesion_too_large` | lesion QC | The volume exceeds the maximum. |
| `bilateral_lesion` | lesion QC | The lesion involves both hemispheres and the run is set to reject those (otherwise it is only a warning). |
| `lesion_outside_parcellation` | flnm-fast, COGNET | The lesion does not overlap any parcel of the parcellation. |
| `mask_outside_atlas_space` | lnm-tracts | No lesion voxel is left after resampling to the atlas space. |
| `disconnection_not_finite` | COGNET | The disconnection map has infinite values. |
| `disconnection_map_undefined` | COGNET | The map has no defined value in any parcel. |
| `disconnection_map_empty` | COGNET | The map is zero wherever it is defined. |

If the message does not explain the problem, send your administrator the link to the run page and the step log.

## 10. How far to trust the results

Every step of the pipeline is **reference-tested**: its output was compared in automated tests with an independent computation on synthetic or public data. **No step is validated**, because no signed validation record exists yet. Therefore:

- Use results for research only, never for clinical decisions.
- Public deployment profiles enable only validated steps, so they offer no analysis yet; the lab instance runs reference-tested steps and marks them **experimental**.
- The tract table and the COGNET profile depend on reference data (atlas, concept maps, cohort) pinned by version; the provenance section of the report lists them. Quote the pipeline and bundle versions when you describe a result.
- The platform cannot verify the declared origin of a mask.
