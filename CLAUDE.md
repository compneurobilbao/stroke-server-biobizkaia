# CLAUDE.md

Claude Code entry point. The rules for every agent are in [`AGENTS.md`](AGENTS.md): read it first and follow it. If anything here or in `.claude/` disagrees with `AGENTS.md` or with the plan ([`docs/plan/IMPLEMENTATION_PLAN.md`](docs/plan/IMPLEMENTATION_PLAN.md)), those win.

The short version:

1. Work on one card of the plan, inside its "Touches" list; propose anything else as a follow-up card.
2. Processing tools enter only as steps that follow the step contract ([`docs/contracts/`](docs/contracts/README.md), [`contracts/`](contracts/), SDK and template in [`steps/`](steps/README.md)).
3. No patient data: synthetic fixtures or public de-identified data with a redistribution-compatible licence only.
4. English everywhere; the UI is English by default and Spanish comes from the i18n catalogues.
5. One branch and one PR per card against `develop`; never merge; CI is the source of truth; ADRs, design and contract changes need owner approval.
6. Never deploy, push to `main` or run destructive commands without explicit owner approval.
7. Before closing: the checks in `AGENTS.md` ("Validation before closing a task"), real results only.

`.claude/commands/` holds slash commands for recurring tasks and `.claude/skills/` holds area guides (backend, frontend, steps, DevOps, documentation, QA, security).
