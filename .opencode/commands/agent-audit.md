---
description: Audits the coherence of code, AGENTS.md, CLAUDE.md, .opencode, .claude, the plan and the docs without editing.
agent: project-architect
subtask: true
---

Audit the agent workflow of this repository.

Scope:

- Compare `AGENTS.md`, `CLAUDE.md`, `opencode.json`, `.opencode/**`, `.claude/**`, `docs/ai-development-rules.md`, `README.md` and `docs/**` against the plan (`docs/plan/IMPLEMENTATION_PLAN.md`), the step contract (`docs/contracts/`) and the code, tests, Makefile, CI workflows, Docker and scripts.
- Find contradictions with the plan, outdated docs, invalid or redundant configuration, and permissions that are too broad.
- Do not edit files unless the user explicitly asks for it.
- Return prioritised findings with concrete evidence and the minimal recommended changes.

Optional arguments: `$ARGUMENTS`
