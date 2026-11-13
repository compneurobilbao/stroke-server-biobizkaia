# Docs

This folder contains the technical and operational documentation of the platform.

## Responsibility

- Explain architecture, deployment, operation, security and maintenance.
- Document the processing pipeline.
- Keep a realistic plan of work.

## Main Documents

- `plan/IMPLEMENTATION_PLAN.md`: implementation plan for the platform and the pipelines (English).
- `adr/`: architecture decision records (English).
- `contracts/`: normative step contract specification (step, pipeline, provenance, bundles, artefact types, versioning).
- `architecture.md`: architecture, components, ER diagram and states.
- `processing-pipeline.md`: API, Redis, worker, adapter, script and PDF flow.
- `roadmap.md`: historical phase-based roadmap (superseded by the implementation plan).
- `configuration.md`: environment variables and use of `.env`.
- `deployment.md`: local installation and Docker Compose deployment.
- `user-manual.md`: manual for functional users.
- `developer-manual.md`: development guide and safe changes by type of modification.
- `operations-manual.md`: daily operation, basic administration and maintenance.
- `security.md`: current security state and pending items.
- `api.md`: main endpoints.
- `testing.md`: available tests.
- `ai-development-rules.md`: rules for AI-assisted development.
- `troubleshooting.md`: common issues.
- `demo-guide.md`: historical note, the script used to demonstrate the prototype in a thesis defence.
- `tfm-technical-summary.md`: historical note, the technical summary written for the master's thesis.
- `agentic-workflow-audit.md`: audit and decisions on OpenCode/agent configuration.

## Maintenance Rule

If architecture, endpoints, deployment, processing flow or security change, this folder must be updated together with the code.
