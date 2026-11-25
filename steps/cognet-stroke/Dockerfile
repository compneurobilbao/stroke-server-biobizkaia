# COGNET-STROKE step image (contract section 6). Build from the repository root:
#   docker build -f steps/cognet-stroke/Dockerfile -t cognet-stroke-step:dev .
# The repository root is the build context because the image bundles the SDK and the contract
# schemas of this repository.
#
# The wrapper consumes the author's repository unmodified at the commit pinned in upstream.env:
# only src/cdr.py (compute_cdr and compute_overlap, numpy only) is needed at run time, copied
# byte for byte to /opt/upstream/COGNET-STROKE/src/cdr.py and imported from there. The
# analysis environment of that repository (concept-map and null-map generation) is not part of
# the image; concept maps and null maps are the concept-maps bundle mounted at /ref/concept_maps.

# ---- The author's code at the commit pinned in upstream.env ----
FROM python:3.12.3-slim-bookworm AS upstream
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY steps/cognet-stroke/upstream.env /tmp/upstream.env
WORKDIR /src
RUN . /tmp/upstream.env \
    && curl -fsSL "https://codeload.github.com/${UPSTREAM_REPOSITORY}/tar.gz/${UPSTREAM_COMMIT}" \
    | tar -xz --strip-components=1 --wildcards '*/src/cdr.py' '*/pyproject.toml' \
    && test -f src/cdr.py \
    && printf '%s\n' "${UPSTREAM_COMMIT}" > UPSTREAM_COMMIT

# ---- Step image ----
FROM python:3.12.3-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts \
    COGNET_STROKE_SRC=/opt/upstream/COGNET-STROKE \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATH="/usr/local/bin:/usr/bin:/bin"

# Pinned dependencies, then the SDK without its own pins; `pip check` fails the build if
# anything became inconsistent.
COPY steps/cognet-stroke/requirements-step.txt /opt/build/requirements-step.txt
COPY steps/sdk /opt/build/sdk
RUN pip install -r /opt/build/requirements-step.txt \
    && pip install --no-deps /opt/build/sdk \
    && pip check \
    && rm -rf /opt/build

# Contract artefacts the SDK and the wrapper read (not copied into the SDK package)
COPY contracts/schemas /opt/step/contracts/schemas
COPY contracts/artefact-types.yaml contracts/validate.py /opt/step/contracts/

# The author's code (unmodified) and the commit it was taken from
COPY --from=upstream /src/src/cdr.py /opt/upstream/COGNET-STROKE/src/cdr.py
COPY --from=upstream /src/pyproject.toml /src/UPSTREAM_COMMIT /opt/upstream/COGNET-STROKE/

# The step: manifest at the contractual location, code, fixed entry point
COPY steps/cognet-stroke/src /opt/step/src
COPY steps/cognet-stroke/step.yaml /opt/step/step.yaml
COPY steps/cognet-stroke/bin/run /usr/local/bin/run

# Empty mount points (Apptainer cannot always create bind targets), world-readable files,
# world-executable entry point, pre-compiled bytecode (the author's file is imported under its
# own module name at run time; its bytecode is not written because the root is read-only).
RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/upstream \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q \
        /opt/step/src /opt/step/contracts /usr/local/lib/python3.12/site-packages/stroke_step_sdk \
    && chmod -R a+rX /opt/step /usr/local/lib/python3.12/site-packages \
    && MPLBACKEND=Agg MPLCONFIGDIR=/dev/shm/mpl python -c \
        "import cognet_stroke_step, stroke_step_sdk, matplotlib.pyplot; from cognet_stroke_step import upstream; upstream.load()" \
    && rm -rf /dev/shm/mpl /tmp/* /root/.cache

# The executor ignores ENTRYPOINT, CMD, WORKDIR and USER and starts
#   /usr/local/bin/run --in /in --out /out --params /in/params.json
# under an arbitrary UID. USER is set for people who run the image by hand.
USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
