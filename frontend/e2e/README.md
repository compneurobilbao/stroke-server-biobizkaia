# End-to-end tests (Playwright)

Tests of the case wizard (both modes) and the run page, in two projects of
`playwright.config.ts`. `@playwright/test` is a pinned devDependency (`1.56.1`), so `npm ci`
installs it.

| Project | Specs | Backend | Runs in |
|---|---|---|---|
| `mocked` | `e2e/mocked/` | none: `mocked/mockApi.ts` answers every `/api` request in the browser, with shapes taken from `docs/api/openapi-v1.json` | job `frontend-e2e` of `ci.yml` |
| `real-stack` | `e2e/real/` | the Docker Compose stack with the `pipelines` profile, the docker executor, built step images and the `ci` implementation profile | workflow `e2e-real-stack.yml` |

The mocked specs check what the UI sends (bodies, upload paths, no exact age, only changed
implementations) and what it shows with scripted responses (defaults, unavailable slots, run
timeline, retry and cancel). The real-stack specs check the same wizard against the real API and
orchestrator:

- **MNI-mask mode** uploads `fixtures/data/mni_masks/lesion_left_medium.nii.gz`, checks the `ci`
  defaults in the slot selector, starts the run, waits (up to 12 minutes, failing early on a
  failed run) until it succeeds, and checks the timeline (real steps plus the
  functional-disconnection stub, normalisation skipped), the artefact list, the PDF download
  (`%PDF-` magic) and the step log. A second run picks a non-default report implementation and
  checks that only that choice is sent and that the run records it as chosen by the user.
- **CT mode** uploads `fixtures/data/cases/case-01` files, and asserts that the normalisation
  slot is shown as unavailable (the CT step is not merged, card F0-33) and that Start is
  disabled. It does not expect a completed run.

The read-only viewer (card F0-32) has its own specs. `mocked/viewer.spec.ts` serves small synthetic NIfTI volumes generated in memory (`mocked/nifti.ts`) and checks, in real Chromium with WebGL, that the viewer opens from the run page, that artefacts are fetched with the token in the header and never in a URL, that no request leaves the application origin (offline requirement), that opacity, visibility and threshold change the drawn picture, and the no-background message. The real-stack MNI-mask spec ends by opening the viewer on the finished run.

## Running the mocked specs locally

```bash
cd frontend
npm ci
npx playwright install chromium
npm run build
npx playwright test --project=mocked
```

## Running the real-stack specs

CI does this in `.github/workflows/e2e-real-stack.yml`; `scripts/e2e/real_stack.sh` holds the
steps that prepare the stack (layout of the build outputs under `data/e2e`, throwaway `.env`,
`docker compose --profile pipelines up`, bundle registration, `seed_implementations --profile ci`,
creation of a throwaway admin and researcher). To reproduce locally you need Docker, the step
images built and pushed to a local registry the way `e2e-stroke-lnm.yml` does it
(`registry.py fetch`, `build`, `materialize`), and the bundles; then:

```bash
export WORK=/path/to/e2e-work          # pipelines/, step-manifests/, bundles/
export E2E_ADMIN_EMAIL=... E2E_ADMIN_PASSWORD=... E2E_USER_EMAIL=... E2E_USER_PASSWORD=...
bash scripts/e2e/real_stack.sh layout && bash scripts/e2e/real_stack.sh env   # overwrites ./.env
bash scripts/e2e/real_stack.sh up && bash scripts/e2e/real_stack.sh bundles
bash scripts/e2e/real_stack.sh seed && bash scripts/e2e/real_stack.sh users
cd frontend
E2E_REAL_STACK=1 PLAYWRIGHT_BASE_URL=http://localhost npx playwright test --project=real-stack
```

The credentials are generated per job and are never committed. The `real-stack` project exists only
when `E2E_REAL_STACK=1` and needs `PLAYWRIGHT_BASE_URL`, `E2E_USER_EMAIL` and `E2E_USER_PASSWORD`.
