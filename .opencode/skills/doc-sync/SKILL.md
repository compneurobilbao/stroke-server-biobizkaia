---
name: doc-sync
description: Keep AGENTS.md, README and docs aligned with real changes without filler documentation.
compatibility: opencode
---

## When to use

Load this skill when architecture, API, deployment, configuration, security, pipeline, agents, skills, commands or permissions change.

## Rules

- `AGENTS.md` is the main source for agents; `CLAUDE.md`, `.claude/`, `.opencode/` and `docs/ai-development-rules.md` must not contradict it.
- The plan (`docs/plan/IMPLEMENTATION_PLAN.md`), the step contract (`docs/contracts/`) and the ADRs (`docs/adr/`) are normative; change them only through a card and with owner approval.
- `README.md` describes the main use and the real structure.
- `docs/architecture.md` and `docs/processing-pipeline.md` reflect the current technical flow; `docs/testing.md` lists the tests and how to run them.
- `docs/agentic-workflow-audit.md` records decisions about agents, skills, commands, plugins and permissions.
- English only; no people's names, host or institution details, or grant references.

## No filler

Do not add new documents when a short section in an existing one is enough. If you create a new file, explain why it exists.
