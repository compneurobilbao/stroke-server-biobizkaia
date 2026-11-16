---
description: Reviews the current diff against its card, AGENTS.md and the project conventions without editing.
agent: code-reviewer
subtask: true
---

Review the current changes of the working tree (or of the branch against `origin/develop`).

Use the card in `docs/plan/IMPLEMENTATION_PLAN.md` and `AGENTS.md` as the reference and check them against the real code. Focus on:

- Bugs, regressions and security risks.
- Secrets, `.env`, credentials, or patient or non-redistributable data.
- Files outside the card's "Touches" list and features the card does not ask for.
- Step contract compliance and architecture drift (tools wired outside the contract, new adapter classes).
- Missing tests, docs or checks; new Spanish text outside the i18n catalogues and report templates.
- Dangerous commands or permissions that are too broad.

Do not edit files. Return prioritised findings with concrete references. If there are none, say so explicitly.

Card or extra context: `$ARGUMENTS`
