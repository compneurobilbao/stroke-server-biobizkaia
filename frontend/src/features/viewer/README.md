# Read-only viewer (card F0-32)

Route `/runs/:runId/view`, opened from the "View images" link of the run page. It shows, in three planes, the artefacts of one run:

| Layer | Artefact type | Rendering |
|---|---|---|
| Background | `ct_mni` (the case CT resampled to MNI152 by the normalisation step) | grey scale, opacity only |
| Lesion mask | `lesion_mask_mni` | blue overlay, opacity, threshold, show/hide |
| Disconnection map | `fdisconnection_map` | warm overlay, opacity, threshold, show/hide |

There is no editing. A run with neither a lesion mask nor a disconnection map shows a message instead of a canvas.

## How the images reach NiiVue

`ViewerPage` lists the run artefacts, downloads each layer with the API client (`downloadArtifact`, bearer token in the `Authorization` header) and passes the bytes to NiiVue as in-memory buffers (`NVImage.loadFromUrl({ url: ArrayBuffer, name })`). No URL ever carries a token; the buffers get a name ending in `.nii.gz` or `.nii` according to their gzip magic number so that NiiVue picks the right reader.

`engine.ts` is the only file that imports NiiVue, and it does so with a dynamic `import()`. The route itself is lazy (`React.lazy` in `App.tsx`), so NiiVue is in a separate chunk that is fetched only when a viewer page is opened; the main bundle is unchanged. Tests replace `engine.ts` (jsdom has no WebGL); the Playwright specs run the real engine in Chromium.

## Offline operation

NiiVue is a pinned npm dependency (`@niivue/niivue` 0.69.0, BSD-2-Clause) bundled by Vite. Its colour maps and fonts are compiled into the library; the viewer requests nothing from another origin. Both Playwright projects assert that no request leaves the origin of the application.

## Background template: decision and licence record

No brain template is bundled. The candidate was the MNI152NLin6Asym 2 mm T1w template of TemplateFlow (the FSL "MNI ICBM 152 non-linear 6th generation" average). Its redistribution terms could not be confirmed: `template_description.json` of the TemplateFlow repository `tpl-MNI152NLin6Asym` says "See LICENSE file", and that repository has no `LICENSE` file; the same image is distributed with FSL under the FSL licence, which restricts commercial use. Without a confirmed licence that allows redistribution, the image is not committed.

Instead:

- when the run has a `ct_mni` artefact (CT mode, once the normalisation step is mounted), it is the background;
- otherwise there is no background, the overlays are drawn on black, and the page says so in the user's language (`viewer.noTemplate`).

If a template with a clear licence is adopted later (for example a template generated from synthetic or CC0 data, or one whose licence is confirmed by its authors), add the file under `frontend/public/`, record its licence and checksum here, and add it as the first layer in `layers.ts` when `ct_mni` is absent.
