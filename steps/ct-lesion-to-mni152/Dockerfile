# Contract step image of the CTLesion2MNI152 wrapper. Build from the repository root:
#   docker build -f steps/ct-lesion-to-mni152/Dockerfile -t ct-lesion-to-mni152-step:dev .
# The repository root is the build context because the image bundles the SDK and the contract
# schemas of this repository. The author's package (CTLesion2MNI152, repository and commit in
# upstream.env) is installed unmodified from that commit; the step needs Python >= 3.10 for the
# SDK and only three FSL programs, so the author's own top-level Dockerfile is not used.

# ---- FSL: only flirt, fslmaths (avwutils) and cluster, pinned to the 6.0.7.6 release set ----
# FSL licence: https://fsl.fmrib.ox.ac.uk/fsl/docs/license.html (non-commercial use).
FROM mambaorg/micromamba:1.5.10-bookworm-slim AS fsl
USER root
RUN micromamba create -y -p /opt/fsl --override-channels \
        -c https://fsl.fmrib.ox.ac.uk/fsldownloads/fslconda/public/ -c conda-forge \
        fsl-base=2309.2 fsl-avwutils=2209.2 fsl-cluster=2201.0 fsl-flirt=2111.2 \
    && micromamba clean -a -y \
    && rm -rf /opt/fsl/pkgs

# ---- The author's package at the commit pinned in upstream.env ----
FROM python:3.11.11-slim-bookworm AS upstream
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY steps/ct-lesion-to-mni152/upstream.env /tmp/upstream.env
WORKDIR /src
RUN . /tmp/upstream.env \
    && curl -fsSL "https://codeload.github.com/${UPSTREAM_REPOSITORY}/tar.gz/${UPSTREAM_COMMIT}" \
    | tar -xz --strip-components=1 --wildcards \
        '*/pyproject.toml' '*/MANIFEST.in' '*/README.md' '*/LICENSE' '*/src/*' \
    && test -f pyproject.toml && test -f src/ct_lesion_to_mni152/registration_workflow.py \
    && printf '%s\n' "${UPSTREAM_COMMIT}" > UPSTREAM_COMMIT

# ---- Step image ----
FROM python:3.11.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts \
    MPLCONFIGDIR=/tmp/matplotlib \
    FSLDIR=/opt/fsl \
    FSLOUTPUTTYPE=NIFTI_GZ \
    FSLMULTIFILEQUIT=TRUE \
    PATH="/usr/local/bin:/usr/bin:/bin:/opt/fsl/bin"

RUN apt-get update -qq && apt-get install -y -q --no-install-recommends libgomp1 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fsl /opt/fsl /opt/fsl

# The algorithm package (unchanged) with its pinned dependencies, then the SDK without its own
# dependency pins (numpy 1.26.4 and nibabel 5.2.1 come from the package; the SDK accepts both).
COPY --from=upstream /src /opt/build/pkg
COPY steps/ct-lesion-to-mni152/requirements-image.txt /opt/build/requirements-image.txt
COPY steps/sdk /opt/build/sdk
RUN pip install -c /opt/build/requirements-image.txt /opt/build/pkg \
    && pip install -r /opt/build/requirements-image.txt \
    && pip install --no-deps /opt/build/sdk \
    && mkdir -p /opt/upstream/CTLesion2MNI152 \
    && cp /opt/build/pkg/UPSTREAM_COMMIT /opt/upstream/CTLesion2MNI152/ \
    && rm -rf /opt/build

COPY contracts/schemas /opt/step/contracts/schemas
COPY contracts/artefact-types.yaml contracts/validate.py /opt/step/contracts/
COPY steps/ct-lesion-to-mni152/src /opt/step/src
COPY steps/ct-lesion-to-mni152/step.yaml /opt/step/step.yaml
COPY steps/ct-lesion-to-mni152/bin/run /usr/local/bin/run

RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/fsl /opt/upstream \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q /opt/step/src /opt/step/contracts \
    && chmod -R a+rX /opt/step \
    && command -v flirt fslmaths fsl-cluster \
    && python -c "import itk, ct_lesion_to_mni152, stroke_step_sdk"

USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
