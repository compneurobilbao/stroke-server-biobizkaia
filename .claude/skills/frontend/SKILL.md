---
name: frontend
description: Guide for frontend work (React + TypeScript + Vite, routed feature modules, react-i18next with English source strings and a Spanish catalogue, Vitest). Use when changing frontend/.
---

# Frontend

- React + TypeScript + Vite in `frontend/`; feature modules in `src/features/` (`auth`, `studies`, `admin`, `notifications`; later `cases`, `viewer`), shared API client in `src/api/`, layout in `src/layout/`, shared components in `src/shared/`.
- All user-facing strings go through i18n (`src/i18n/`): English is the source language and the default; add every new key to the Spanish catalogue as well. Do not hard-code text in components.
- Key API error handling on the machine-readable `code`, not on the `detail` text.
- Dependencies are pinned and the lockfile is committed; do not add CDN assets (the viewer must work offline).
- Show clear state (queued, running, failed, cancelled) and keep the research-use notice visible where results are shown.
- From `frontend/`: `npm run typecheck`, `npm run lint`, `npm test` (Vitest), `npm run build`.
