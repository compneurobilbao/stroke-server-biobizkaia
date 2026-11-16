# OpenCode configuration

OpenCode tooling for this repository. The rules are in [`AGENTS.md`](../AGENTS.md), which `opencode.json` loads as instructions; nothing here replaces it, the implementation plan (`docs/plan/IMPLEMENTATION_PLAN.md`) or the step contract (`docs/contracts/`).

## Layout

- `agents/`: subagents with defensive permissions: `project-architect` (analysis and design against the plan), `doc-steward` (documentation and hand-offs), `code-reviewer` (read-only review of a diff against its card).
- `commands/`: repeatable routines: `/agent-audit`, `/handoff`, `/review-diff`.
- `skills/`: on-demand instructions: `repo-orientation`, `safe-change`, `doc-sync`.
- `plugins/`: `secret-protection.js` blocks reads of `.env`, credentials, keys and certificates (example files stay readable).

Project-wide permissions (which commands are allowed, asked for or denied) are in the root `opencode.json`. Changes to this folder are recorded in `docs/agentic-workflow-audit.md`.

## Note

This folder is not part of the Docker runtime. If OpenCode generates `node_modules`, `package.json`, `package-lock.json` or `bun.lock` here, they are ignored and must not be committed.
