# AI-assisted development rules

[`AGENTS.md`](../AGENTS.md) is the authoritative rule set for AI agents; this page summarises the stable rules for readers of the documentation. If the two disagree, `AGENTS.md` and the [implementation plan](plan/IMPLEMENTATION_PLAN.md) win.

1. Work follows the task cards of the implementation plan: one card, one branch, one pull request against `develop`, changes limited to the card's "Touches" list.
2. Processing tools are added only as steps that follow the step contract ([`docs/contracts/`](contracts/README.md), schemas and validator in `contracts/`), built with the step SDK and template in `steps/`.
3. No patient data. Tests, CI and demos use synthetic fixtures or public de-identified data whose licence allows redistribution.
4. English everywhere. The UI is English by default; Spanish is available through the i18n catalogues.
5. Agents never merge pull requests. CI is the source of truth. ADRs, design, contract and plan changes need approval from the project owner.
6. Deployment happens only from a tagged commit and only with explicit owner approval for that tag. `main` is a fast-forward of tagged `develop` commits.
7. No secrets in the repository; `.env` is never read or committed by agents.
8. Functional changes come with tests and with updates to the affected documentation in the same pull request.
9. Changes to agent tooling and rules are recorded in [`agentic-workflow-audit.md`](agentic-workflow-audit.md).
