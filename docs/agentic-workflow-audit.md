# Agentic workflow audit

Record of decisions about the agent configuration of this repository: `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.opencode/`, `opencode.json` and `docs/ai-development-rules.md`. Newest entry first.

## 2026-11-13: rules aligned with the implementation plan (card F0-06)

### Why

The previous rules were written for the single-file prototype and contradicted the implementation plan ([`docs/plan/IMPLEMENTATION_PLAN.md`](plan/IMPLEMENTATION_PLAN.md)): they were in Spanish, required the GUI to stay in Spanish, forbade features the plan needs (authentication, roles, TLS, object storage, retention, roadmap items), let OpenCode push straight to `develop`, and required `main` to be free of agent files, which forced commits to be re-created on `main` with different hashes (plan section 1.3).

### Decisions

- All agent files are in English.
- `AGENTS.md` is the single rule set and points to the plan, the step contract (`docs/contracts/`, `contracts/`), the ADRs (`docs/adr/`), the testing doc (`docs/testing.md`) and the step SDK (`steps/README.md`). `CLAUDE.md`, `docs/ai-development-rules.md`, `.claude/` and `.opencode/` only refine or summarise it.
- The list of forbidden features is replaced by "follow the plan's cards": an agent implements its card inside the card's "Touches" list and proposes everything else as a follow-up card.
- Processing tools are added only as steps that follow the step contract; `processor_adapter/` is a legacy boundary and receives no new adapters.
- No patient data: synthetic fixtures or public de-identified data with a redistribution-compatible licence only (plan section 6).
- The "GUI in Spanish" rule is removed: the UI is English by default with i18n, Spanish available through the catalogues.
- Branch and review protocol from plan section 7: one branch and one PR per card against `develop`; agents never merge; CI is the source of truth; ADRs, design, contract and plan changes and every deployment need owner approval.
- Git: the rule that kept agent files off `main` is dropped. Agent files live on both branches; `main` is a fast-forward of tagged `develop` commits.
- Deployment only from a tagged commit with explicit owner approval, following the sequence in plan section 7.
- The technical guidance was updated to the current repository: routers in `backend/app/api/`, `orchestrator/executors/`, `steps/`, `contracts/`, `fixtures/`, the TypeScript frontend with i18n, and the Makefile targets including `make test-steps` and `make e2e`.

### Tool changes

- `opencode.json`: `git push origin develop` changed from `allow` to `deny` (work reaches `develop` through reviewed PRs only); pushes to `main`, force pushes and `gh pr merge` are denied; other pushes stay `ask`. `make test-steps`, `python contracts/validate.py` and the frontend check scripts are allowed; `make e2e` asks. `.env.example` files are readable again (they hold no secrets and agents edit them).
- `.opencode/plugins/secret-protection.js`: `*.example` files are no longer blocked.
- `.opencode/` agents, commands and skills translated and pointed at the plan and the contract; the code reviewer checks the card's "Touches" list and the definition of done.
- `.claude/`: commands translated and updated (`plan-card` replaces `plan-feature`; new `implement-step`); the area guides moved to the Claude Code skill layout `.claude/skills/<name>/SKILL.md`, with a new `steps` skill.

## 2026-06-08: scoped push permissions (superseded)

Scoped permissions were added to `opencode.json` for `git add *`, `git commit *` and `git push origin develop`, so that OpenCode could finish commits and pushes to `develop` when the user asked. Force pushes and destructive commands stayed blocked. The `develop` push permission was withdrawn on 2026-11-13.

## 2026-04-29: initial OpenCode configuration (partly superseded)

### Starting point

- The project was configured for Claude Code and OpenCode; `AGENTS.md`, `CLAUDE.md` and `docs/ai-development-rules.md` repeated similar rules.
- `.opencode/` had seven agents and nine very short commands without usable frontmatter, no root `opencode.json` and no skills.

### Decisions (still valid unless superseded above)

- `AGENTS.md` is the main source for agents; `CLAUDE.md` is a compatibility bridge.
- OpenCode is configured by `opencode.json` with model `openai/gpt-5.5`.
- Three OpenCode agents: `project-architect` (analysis, design, risks), `doc-steward` (documentation and hand-offs), `code-reviewer` (read-only review).
- Three commands: `/agent-audit`, `/handoff`, `/review-diff`.
- Three skills: `repo-orientation`, `safe-change`, `doc-sync`.
- A small local plugin, `secret-protection.js`, blocks accidental reads of secrets.
- No agents were created per area (backend, frontend, DevOps, QA, security) and no commands duplicate Makefile targets.
- `.opencode/node_modules/` and other OpenCode-generated dependency files stay ignored (`.opencode/.gitignore`).
- *Superseded on 2026-11-13:* the rule that `main` must contain no agent files.

## Open risks

- Check with `opencode models` that `openai/gpt-5.5` is available with the local credentials.
- Pattern-based permissions are a safety net, not a guarantee; the protocol in `AGENTS.md`, CI and review on GitHub remain the real controls.
