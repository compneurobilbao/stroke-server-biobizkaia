---
description: Plan the implementation of one card of the implementation plan before touching code.
---

# Plan a card

Card: $ARGUMENTS

1. Read `AGENTS.md`, then the card in `docs/plan/IMPLEMENTATION_PLAN.md` (search for its ID), the decisions it builds on (section 2), the cards it depends on, section 6 (test data) and section 7 (review protocol).
2. Read the normative documents the card relies on: the step contract (`docs/contracts/`), the relevant ADRs (`docs/adr/`), `docs/testing.md`.
3. Read the code the card touches and confirm it matches what the card assumes; report any mismatch.
4. Propose a short plan: files to change (all inside the card's "Touches" list), tests to add, docs to update, how each acceptance criterion will be shown in the PR, risks, and anything that needs owner approval (ADR, design, contract or plan change).
5. List what is out of scope and would become follow-up card stubs.

Do not edit files while planning.
