# Claude Code configuration

Slash commands and skills for Claude Code in this repository. They are working aids, not platform code, and they only refine [`AGENTS.md`](../AGENTS.md): the rules, the sources of truth (plan, step contract, ADRs, testing doc) and the review protocol are there.

## Layout

- `commands/`: short instructions for recurring tasks, used as `/<name>`:
  `plan-card`, `implement-backend-endpoint`, `implement-worker-task`, `implement-step`, `review-architecture`, `run-quality-checks`, `security-review`, `update-docs`, `generate-manual`, `prepare-release`.
- `skills/<name>/SKILL.md`: area guides loaded when relevant: `backend`, `frontend`, `steps`, `devops`, `documentation`, `qa`, `security`.

## Rule

When the architecture, the plan or the rules change, keep this folder aligned with `AGENTS.md`, `CLAUDE.md` and `docs/ai-development-rules.md`, and record the change in `docs/agentic-workflow-audit.md`.
