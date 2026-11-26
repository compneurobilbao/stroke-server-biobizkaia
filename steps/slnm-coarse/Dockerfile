# syntax=docker/dockerfile:1
# Step image of the slnm-coarse wrapper (contract section 6). Build from the repository root:
#   docker build -f steps/slnm-coarse/Dockerfile -t slnm-coarse-step:dev .
# The repository root is the build context because the image bundles the SDK and the contract
# schemas of this repository.
#
# The wrapper consumes the author's repository (fast-slnm, MIT) unmodified at the commit pinned
# in upstream.env. The image follows the author's own Dockerfile at that commit: Ubuntu 24.04
# with its Python 3.12, ANTs antsApplyTransforms from the ANTs 2.6.3 release binary (checksum
# pinned, licence files copied), and the slnm-fast package installed with uv from the author's
# lock file (`uv sync --locked --no-dev`). Only the coarse workflow is used, so MRtrix3 (needed
# by the author's preparation and indexed-TDI commands) is not installed.
#
# While the author's repository is private, the archive is read through the GitHub API with a
# BuildKit secret (never stored in a layer):
#   docker build --secret id=upstream_token,env=UPSTREAM_TOKEN -f steps/slnm-coarse/Dockerfile ...
# Without the secret the public archive (codeload.github.com) is used, as for the other wrappers.

# ---- ANTs: antsApplyTransforms only (as in the author's Dockerfile) ----
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b AS ants
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /tmp/ants.zip \
      https://github.com/ANTsX/ANTs/releases/download/v2.6.3/ants-2.6.3-ubuntu-24.04-X64-gcc.zip \
    && echo '5ddbf80c2daa3c8fa46b60d477fdb22a34b39900f8492efb21b242d1fbfa5a53  /tmp/ants.zip' | sha256sum -c - \
    && mkdir -p /opt/ants/bin \
    && unzip -j /tmp/ants.zip '*/bin/antsApplyTransforms' -d /opt/ants/bin \
    && rm /tmp/ants.zip
ADD --checksum=sha256:c8634de3c336f46549faf59ade8bc473d357fdd2c05b9e189d0f0633264894a3 https://raw.githubusercontent.com/ANTsX/ANTs/v2.6.3/ANTSCopyright.txt /opt/ants/licenses/ANTSCopyright.txt
ADD --checksum=sha256:c6596eb7be8581c18be736c846fb9173b69eccf6ef94c5135893ec56bd92ba08 https://raw.githubusercontent.com/ANTsX/ANTs/v2.6.3/COPYING.txt /opt/ants/licenses/COPYING.txt

# ---- The author's code at the commit pinned in upstream.env ----
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b AS upstream
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY steps/slnm-coarse/upstream.env /tmp/upstream.env
COPY steps/upstream-digest.sh /tmp/upstream-digest.sh
WORKDIR /src
RUN --mount=type=secret,id=upstream_token \
    . /tmp/upstream.env \
    && if [ -s /run/secrets/upstream_token ]; then \
         curl -fsSL -H "Authorization: Bearer $(cat /run/secrets/upstream_token)" -o /tmp/upstream.tar.gz \
           "https://api.github.com/repos/${UPSTREAM_REPOSITORY}/tarball/${UPSTREAM_COMMIT}"; \
       else \
         curl -fsSL -o /tmp/upstream.tar.gz \
           "https://codeload.github.com/${UPSTREAM_REPOSITORY}/tar.gz/${UPSTREAM_COMMIT}"; \
       fi \
    && tar -xz --strip-components=1 -f /tmp/upstream.tar.gz --wildcards \
        '*/pyproject.toml' '*/uv.lock' '*/README.md' '*/LICENSE' '*/src/*' \
    && rm /tmp/upstream.tar.gz \
    && test -f src/slnm_fast/coarse/cohort_coarse_lesion.py \
    && test "$(sh /tmp/upstream-digest.sh . $UPSTREAM_PATHS)" = "$UPSTREAM_SHA256" \
    && printf '%s\n' "${UPSTREAM_COMMIT}" > UPSTREAM_COMMIT

# ---- Step image ----
FROM ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b
RUN apt-get update -qq && apt-get install -y -q --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ants /opt/ants/bin/antsApplyTransforms /usr/local/bin/
COPY --from=ants /opt/ants/licenses/ /usr/share/doc/ants/
COPY --from=ghcr.io/astral-sh/uv:0.4.30@sha256:341e448d2ca38f11d8e2768db5464b4c95a4d87f539b8cb7511db86b02fef97e /uv /usr/local/bin/uv
# Thread limits as in the author's image: the author's code runs one single-threaded
# antsApplyTransforms per worker process, and the step's `workers` parameter sets the processes.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    OPENBLAS_NUM_THREADS=1 \
    ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1 \
    OMP_NUM_THREADS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"

# The author's package (unmodified) with the dependencies of its lock file (numpy, nibabel),
# then the SDK without its own pins and the few packages it adds; `uv pip check` fails the
# build if anything became inconsistent. The package source is not kept in the image (it is
# installed, non-editable); its LICENSE and the commit are kept next to it.
COPY --from=upstream /src /opt/build/pkg
COPY steps/slnm-coarse/requirements-step.txt /opt/build/requirements-step.txt
COPY steps/sdk /opt/build/sdk
RUN cd /opt/build/pkg \
    && uv sync --locked --no-dev --no-editable --no-cache \
    && uv pip install --no-cache --python /opt/venv/bin/python -r /opt/build/requirements-step.txt \
    && uv pip install --no-cache --python /opt/venv/bin/python --no-deps /opt/build/sdk \
    && uv pip check --python /opt/venv/bin/python \
    && mkdir -p /opt/upstream/fast-slnm \
    && cp /opt/build/pkg/LICENSE /opt/build/pkg/UPSTREAM_COMMIT /opt/upstream/fast-slnm/ \
    && rm -rf /opt/build /root/.cache

# Contract artefacts the SDK reads (not copied into the SDK package)
COPY contracts/schemas /opt/step/contracts/schemas
COPY contracts/artefact-types.yaml contracts/validate.py /opt/step/contracts/

# The step: manifest at the contractual location, code, fixed entry point
COPY steps/slnm-coarse/src /opt/step/src
COPY steps/slnm-coarse/step.yaml /opt/step/step.yaml
COPY steps/slnm-coarse/bin/run /usr/local/bin/run

# Empty mount points (Apptainer cannot always create bind targets), world-readable files,
# world-executable entry point, pre-compiled bytecode; the executables actually start
# (shared libraries included).
RUN mkdir -p /in /out /ref /tmp \
    && find /tmp -mindepth 1 -delete \
    && chmod 0755 /in /out /ref /usr/local/bin/run /usr/local/bin/antsApplyTransforms \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/upstream /opt/venv /usr/share/doc/ants \
    && PYTHONDONTWRITEBYTECODE= /opt/venv/bin/python -m compileall -q \
        /opt/step/src /opt/step/contracts /opt/venv/lib/python3.12/site-packages \
    && chmod -R a+rX /opt/step /opt/venv \
    && antsApplyTransforms --help > /dev/null \
    && slnm-coarse --help > /dev/null \
    && /opt/venv/bin/python -c "import slnm_fast.coarse.cohort_coarse_lesion, slnm_coarse_step, stroke_step_sdk"

# The executor ignores ENTRYPOINT, CMD, WORKDIR and USER and starts
#   /usr/local/bin/run --in /in --out /out --params /in/params.json
# under an arbitrary UID. USER is set for people who run the image by hand.
USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
