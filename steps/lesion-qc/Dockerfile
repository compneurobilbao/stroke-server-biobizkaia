# Lesion-qc step image (contract section 6). Build from the repository root:
#   docker build -f steps/lesion-qc/Dockerfile -t lesion-qc-step:dev .
# The repository root is the build context because the image bundles the SDK and the
# contract schemas from this repository.
FROM python:3.12.8-slim-bookworm

# PYTHONDONTWRITEBYTECODE: the root file system is read-only at run time; bytecode is
# pre-compiled below instead. STEP_CONTRACTS_DIR tells the SDK where the schemas are.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts \
    MPLCONFIGDIR=/tmp/matplotlib

# SDK with pinned dependencies
COPY steps/sdk /opt/build/sdk
RUN pip install -r /opt/build/sdk/requirements-image.txt \
    && pip install --no-deps /opt/build/sdk \
    && rm -rf /opt/build

# Contract artefacts the SDK reads (not copied into the SDK package)
COPY contracts/schemas /opt/step/contracts/schemas
COPY contracts/artefact-types.yaml contracts/validate.py /opt/step/contracts/

# The step: manifest at the contractual location, code, fixed entry point
COPY steps/lesion-qc/requirements-step.txt /opt/build/requirements-step.txt
RUN pip install -r /opt/build/requirements-step.txt && rm -rf /opt/build
COPY steps/lesion-qc/src /opt/step/src
COPY steps/lesion-qc/lesion_qc.schema.json /opt/step/lesion_qc.schema.json
COPY steps/lesion-qc/step.yaml /opt/step/step.yaml
COPY steps/lesion-qc/bin/run /usr/local/bin/run

# Empty mount points (Apptainer cannot always create bind targets), world-readable
# files, world-executable entry point, pre-compiled bytecode.
RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q \
        /opt/step/src /opt/step/contracts /usr/local/lib/python3.12/site-packages/stroke_step_sdk \
    && chmod -R a+rX /opt/step /usr/local/lib/python3.12/site-packages/stroke_step_sdk

# The executor ignores ENTRYPOINT, CMD, WORKDIR and USER and starts
#   /usr/local/bin/run --in /in --out /out --params /in/params.json
# under an arbitrary UID. USER is set for people who run the image by hand.
USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
