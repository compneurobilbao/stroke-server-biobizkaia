# flnm-fast step image (contract section 6). Build from the repository root:
#   docker build -f steps/flnm-fast/Dockerfile -t flnm-fast-step:dev .
# The repository root is the build context because the image bundles the SDK and the
# contract schemas from this repository.
#
# The base is the public engine image, used unchanged and pinned by digest (it has no source
# repository; the image is the deliverable). This file only adds the SDK, the contract
# wrapper and the manifest on top, as small layers: the engine's own Python, numpy, nibabel,
# pandas and scipy are not reinstalled or upgraded.
FROM compneurobilbaolab/flnm-fast@sha256:7370e5d9ee5c67fe7e7cf1bf6ae94d4579ee0fcf07cbf99bd3e5a39af2fd8173

# PYTHONDONTWRITEBYTECODE: the root file system is read-only at run time; bytecode is
# pre-compiled below instead. STEP_CONTRACTS_DIR tells the SDK where the schemas are.
# (PYTHONUNBUFFERED, PIP_NO_CACHE_DIR and PIP_DISABLE_PIP_VERSION_CHECK come from the base.)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/step/src \
    STEP_CONTRACTS_DIR=/opt/step/contracts

# SDK without its dependencies (numpy and nibabel are in the base), plus the few pure Python
# or small wheels it still needs; `pip check` fails the build if anything became inconsistent.
COPY steps/sdk /opt/build/sdk
COPY steps/flnm-fast/requirements-step.txt /opt/build/requirements-step.txt
RUN pip install -r /opt/build/requirements-step.txt \
    && pip install --no-deps /opt/build/sdk \
    && pip check \
    && rm -rf /opt/build

# Contract artefacts the SDK reads (not copied into the SDK package)
COPY contracts/schemas /opt/step/contracts/schemas
COPY contracts/artefact-types.yaml contracts/validate.py /opt/step/contracts/

# The step: manifest at the contractual location, code, fixed entry point
COPY steps/flnm-fast/src /opt/step/src
COPY steps/flnm-fast/step.yaml /opt/step/step.yaml
COPY steps/flnm-fast/bin/run /usr/local/bin/run

# Empty mount points (Apptainer cannot always create bind targets), world-readable
# files, world-executable entry point (and engine), pre-compiled bytecode.
RUN mkdir -p /in /out /ref /tmp \
    && chmod 0755 /in /out /ref /usr/local/bin/run \
    && chmod 1777 /tmp \
    && chmod -R a+rX /opt/step /opt/lesion_zmaps \
    && PYTHONDONTWRITEBYTECODE= python -m compileall -q \
        /opt/step/src /opt/step/contracts /usr/local/lib/python3.12/site-packages/stroke_step_sdk \
    && chmod -R a+rX /opt/step /usr/local/lib/python3.12/site-packages/stroke_step_sdk

# The executor ignores ENTRYPOINT, CMD, WORKDIR and USER and starts
#   /usr/local/bin/run --in /in --out /out --params /in/params.json
# under an arbitrary UID. The engine's ENTRYPOINT is cleared so that the image behaves like
# every other step image; USER is set for people who run the image by hand.
ENTRYPOINT []
USER 10001:10001
WORKDIR /tmp
CMD ["/usr/local/bin/run", "--help"]
