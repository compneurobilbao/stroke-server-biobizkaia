# ct-lesion-to-mni152: CT lesion mask to MNI152

Pipeline slot `normalisation`. Registers a non-contrast head CT to the MNI152 template and carries
the lesion mask with it, with the unchanged registration workflow of the author's
[CTLesion2MNI152](https://github.com/compneurobilbao/CTLesion2MNI152) package; the step then
optionally removes the overlap with an age-matched ventricle template and reduces the mask to
the 2 mm working grid. Manifest: [`step.yaml`](step.yaml).

## Where the code lives

This wrapper is platform code and lives here, not in the author's repository. The
CTLesion2MNI152 package is installed **unmodified** from the commit pinned in
[`upstream.env`](upstream.env) (the registry entry repeats it as `source.upstream`;
`pipelines/stroke-lnm/registry.py check` keeps the two equal). The package runs its FSL commands
with `subprocess.call` and ignores their exit codes; inside the step only, the wrapper swaps in
`check_call` while the workflow runs (`src/ct_lesion_step/step.py`), and the package itself is
left as it is.

Version 1.0.1 is version 1.0.0 (previously built from the `step/` folder of the author's
repository at that same commit) moved into this repository: same wrapper code, same package,
same FSL and Python packages, same results. It is a new version only because the image is
rebuilt (a step version identifies one image). The manifest also names the image repository
the step is actually published to (`step-ct-lesion-to-mni152`; 1.0.0 named a repository that was
never used).

## Licences

- The wrapper is MIT, like the rest of the platform. The CTLesion2MNI152 package is MIT.
- **FSL** (`flirt`, `fslmaths`, `cluster`, from the FSL conda channel, release set 6.0.7.6) is in
  the image. Its licence is non-commercial for the general public
  (https://fsl.fmrib.ox.ac.uk/fsl/docs/license.html). The platform is non-commercial research
  software, and the project owner accepts publishing this image on the public registry on that
  basis (`licence_review` of the registry entry in `pipelines/stroke-lnm/registry.yaml`); anyone
  who uses the image for commercial purposes needs their own FSL licence.
- ITK and itk-elastix (Apache-2.0), nibabel (MIT), nilearn (BSD-3-Clause) and matplotlib come in
  with the package's pinned dependencies; the list with versions is `licence.third_party` of the
  manifest.
- The CT, skull and ventricle templates shipped inside the package are part of the author's
  repository at the pinned commit.

## Reference case and CI

The reference case is a real public head CT template with a known transform: CI downloads the
High Resolution CT Template (Muschelli 2020, derived from the CQ500 scans; CC BY-NC-SA 4.0,
de-identified research data, used for non-commercial tests only) at a pinned commit with SHA-256
checks (`ci/fetch_reference_data.sh`), and `ci/make_reference.py` builds the case with a fixed seed
(nothing is committed; `pipelines/stroke-lnm/ci/ct_reference.sh` runs both for the pipeline
end-to-end test and the release). The expected result is a regression floor (Dice 0.40), not an
accuracy claim.

The job `ct-lesion-to-mni152` of [`.github/workflows/steps.yml`](../../.github/workflows/steps.yml)
runs the unit tests with the author's package checked out at the pinned commit, builds the image,
runs the reference test under Docker and Apptainer, the `skull_not_found` rejection on the
low-resolution synthetic fixture and the ventricle-removal run, and **compares the image with
the previously published one** (`step-ct-lesion-to-mni152` 1.0.0, by digest) on the reference
case: the lesion masks must be byte-identical.

Local image: `docker build -f steps/ct-lesion-to-mni152/Dockerfile -t ct-lesion-to-mni152-step:dev .`
from the repository root.
