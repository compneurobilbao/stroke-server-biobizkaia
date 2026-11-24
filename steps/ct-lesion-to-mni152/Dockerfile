# Contract step image for CTLesion2MNI152 (card F0-23). Build from the repository root:
#   docker build -f step/Dockerfile -t ct-lesion-to-mni152-step:dev .
# The existing top-level Dockerfile (full FSL on neurodebian, Python 3.9) is left untouched;
# the step needs Python >= 3.10 for the SDK and only three FSL programs.

# ---- FSL: only flirt, fslmaths (avwutils) and cluster, pinned to the 6.0.7.6 release set ----
# FSL licence: https://fsl.fmrib.ox.ac.uk/fsl/docs/license.html (non-commercial use).
FROM mambaorg/micromamba:1.5.10-bookworm-slim AS fsl
USER root
RUN micromamba create -y -p /opt/fsl --override-channels \
        -c https://fsl.fmrib.ox.ac.uk/fsldownloads/fslconda/public/ -c conda-forge \
        fsl-base=2309.2 fsl-avwutils=2209.2 fsl-cluster=2201.0 fsl-flirt=2111.2 \
    && micromamba clean -a -y \
    && rm -rf /opt/fsl/pkgs

# ---- Contract SDK and contract files, pinned to a commit of the public stroke-server repo ----
FROM python:3.11.11-slim-bookworm AS contract
ARG STROKE_SERVER_SHA=e360c91961dada08b2559618b90f4bc007c1e9ed
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL "https://codeload.github.com/compneurobilbao/stroke-server-biobizkaia/tar.gz/${STROKE_SERVER_SHA}" \
    | tar -xz --strip-components=1 --wildcards '*/steps/sdk' '*/contracts' \
    && test -f steps/sdk/pyproject.toml && test -f contracts/validate.py

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

# The algorithm package (unchanged apart from the checked subprocess calls) with its pinned
# dependencies, then the SDK without its own dependency pins (numpy 1.26.4 and nibabel 5.2.1
# come from the package; the SDK accepts both).
COPY pyproject.toml MANIFEST.in README.md LICENSE /opt/build/pkg/
COPY src /opt/build/pkg/src
COPY step/requirements-image.txt /opt/build/requirements-image.txt
COPY --from=contract /src/steps/sdk /opt/build/sdk
RUN pip install -c /opt/build/requirements-image.txt /opt/build/pkg \
    && pip install -r /opt/build/requirements-image.txt \
    && pip install --no-deps /opt/build/sdk \
    && rm -rf /opt/build

COPY --from=contract /src/contracts/schemas /opt/step/contracts/schemas
COPY --from=contract /src/contracts/artefact-types.yaml /src/contracts/validate.py /opt/step/contracts/
COPY step/src /opt/step/src
COPY step/step.yaml /opt/step/step.yaml
COPY step/bin/run /usr/local/bin/run

RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/fsl \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q /opt/step/src /opt/step/contracts \
    && chmod -R a+rX /opt/step \
    && command -v flirt fslmaths fsl-cluster \
    && python -c "import itk, ct_lesion_to_mni152, stroke_step_sdk"

USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
