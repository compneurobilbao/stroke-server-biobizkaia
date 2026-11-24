# Contract step image of the tract-wise disconnection step (card F0-25). Build from the
# repository root:
#   docker build -f step/Dockerfile -t lnm-tracts-step:dev .
# The repository root is the build context so that the legacy stage can carry src/lnm_tracts.sh.
# The existing top-level Dockerfile (full dwi-processing image) is left untouched; this step
# needs two MRtrix3 commands and the step SDK.

# ---- MRtrix3: only tckedit and tckstats, compiled statically from the pinned release source ----
# MRtrix3 is MPL-2.0: https://github.com/MRtrix3/mrtrix3/blob/3.0.4/LICENCE.txt
FROM python:3.12.8-slim-bookworm AS mrtrix
ARG MRTRIX_VERSION=3.0.4
ARG MRTRIX_SHA256=f1d1aa289cfc3e46e3a8eca93594b23d061c6d50a0cd03727433a7e2cd14f71a
RUN apt-get update -qq \
    && apt-get install -y -q --no-install-recommends \
        ca-certificates curl g++ make pkg-config libeigen3-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL -o mrtrix3.tar.gz "https://github.com/MRtrix3/mrtrix3/archive/refs/tags/${MRTRIX_VERSION}.tar.gz" \
    && echo "${MRTRIX_SHA256}  mrtrix3.tar.gz" | sha256sum -c - \
    && tar -xz --strip-components=1 -f mrtrix3.tar.gz \
    && ./configure -nogui -noshared \
    && ./build bin/tckedit bin/tckstats \
    && mkdir -p /opt/mrtrix3/bin \
    && cp bin/tckedit bin/tckstats /opt/mrtrix3/bin/ \
    && /opt/mrtrix3/bin/tckedit -version | head -3

# ---- Contract SDK and contract files, pinned to a commit of the public stroke-server repo ----
FROM python:3.12.8-slim-bookworm AS contract
ARG STROKE_SERVER_SHA=1676a391b1d2f3fbfad9b48a416db6978dd5e92e
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL "https://codeload.github.com/compneurobilbao/stroke-server-biobizkaia/tar.gz/${STROKE_SERVER_SHA}" \
    | tar -xz --strip-components=1 --wildcards '*/steps/sdk' '*/contracts' \
    && test -f steps/sdk/pyproject.toml && test -f contracts/validate.py

# ---- Runtime base: Python and the two MRtrix3 commands ----
FROM python:3.12.8-slim-bookworm AS runtime
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends zlib1g libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=mrtrix /opt/mrtrix3/bin /opt/mrtrix3/bin
ENV PATH="/opt/mrtrix3/bin:/usr/local/bin:/usr/bin:/bin"

# ---- Legacy check (CI only, never the default target): the former script with its bc dependency.
#   docker build -f step/Dockerfile --target legacy -t lnm-tracts-legacy:dev .
#   docker run --rm -v PROJECT:/work lnm-tracts-legacy:dev bash /opt/legacy/lnm_tracts.sh /work
FROM runtime AS legacy
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends bc \
    && rm -rf /var/lib/apt/lists/*
COPY src/lnm_tracts.sh /opt/legacy/lnm_tracts.sh

# ---- Step image (default target) ----
FROM runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts

# SDK with pinned dependencies
COPY --from=contract /src/steps/sdk /opt/build/sdk
RUN pip install -r /opt/build/sdk/requirements-image.txt \
    && pip install --no-deps /opt/build/sdk \
    && rm -rf /opt/build

# Contract artefacts the SDK reads (not copied into the SDK package)
COPY --from=contract /src/contracts/schemas /opt/step/contracts/schemas
COPY --from=contract /src/contracts/artefact-types.yaml /src/contracts/validate.py /opt/step/contracts/

# The step: manifest at the contractual location, code, fixed entry point
COPY step/src /opt/step/src
COPY step/step.yaml /opt/step/step.yaml
COPY step/bin/run /usr/local/bin/run

# Empty mount points (Apptainer cannot always create bind targets), world-readable files,
# world-executable entry point, pre-compiled bytecode.
RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/mrtrix3 \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q \
        /opt/step/src /opt/step/contracts /usr/local/lib/python3.12/site-packages/stroke_step_sdk \
    && chmod -R a+rX /opt/step /usr/local/lib/python3.12/site-packages/stroke_step_sdk \
    && command -v tckedit tckstats \
    && python -c "import nibabel, stroke_step_sdk"

# The executor ignores ENTRYPOINT, CMD, WORKDIR and USER and starts
#   /usr/local/bin/run --in /in --out /out --params /in/params.json
# under an arbitrary UID. USER is set for people who run the image by hand.
USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
