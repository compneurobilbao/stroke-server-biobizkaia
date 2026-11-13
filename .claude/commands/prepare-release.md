---
description: Prepare a release candidate on develop for owner approval; never tag, push to main or deploy.
---

# Prepare a release

Version: $ARGUMENTS

Check: CI green on `develop`; `CHANGELOG.md` complete for the version; semantic version consistent; docs updated; `.env.example` complete and free of real values; no secrets (`make check-secrets`, gitleaks); migrations have tested downgrades; step and pipeline references pinned by digest.

Report the result and the proposed tag. Tagging, the fast-forward of `main` to the tagged `develop` commit and any deployment are done only with explicit owner approval (`AGENTS.md`, "Deployment").
