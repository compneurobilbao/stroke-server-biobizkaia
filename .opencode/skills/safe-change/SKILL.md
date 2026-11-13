---
name: safe-change
description: Protocol for small, reversible and validated changes in this project.
compatibility: opencode
---

## When to use

Load this skill before changing code, configuration or the agent workflow.

## Protocol

1. Read the card and the minimal set of files needed to understand the current state.
2. Touch as few files as possible, all inside the card's "Touches" list.
3. Do not revert changes made by others in the working tree.
4. Do not read `.env`, credentials or any real data; use synthetic fixtures.
5. Do not run destructive commands, deployments, pushes to `develop` or `main`, merges, migrations or clean-ups without explicit permission.
6. After editing, review `git diff`.
7. Run the focused checks from `AGENTS.md` ("Validation before closing a task") for the areas you changed.
8. Explain what was checked, with real results, and what is still pending; CI on the PR is the source of truth.

## Criterion

If a solution needs backwards-compatibility layers, new abstractions or a lot of new code, first justify the need with evidence from the card or the code.
