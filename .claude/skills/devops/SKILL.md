---
name: devops
description: Guide for Docker Compose, Makefile, CI workflows, executors' infrastructure and deployment rules. Use when changing docker-compose.yml, Dockerfiles, infra/, scripts/, .github/workflows/ or deployment docs.
---

# DevOps

- One Docker Compose project (`docker-compose.yml`, local overrides in `docker-compose.override.yml`); Nginx entry in `infra/reverse-proxy/`; Make targets in `Makefile` (`up`, `down`, `rebuild`, `migrate`, `smoke`, `backup`, `restore`, `e2e`, ...).
- Keep `.env.example` complete and free of real values; secrets live only in the server's environment. Healthchecks, volumes and the proxy stay simple and auditable.
- Deployment profiles (D14), the `docker` executor's socket proxy and image allow-list (D3), and the deployment runbook belong to their cards (F0-14, F0-40).
- CI: `.github/workflows/ci.yml` and `steps.yml`. A PR is done only when every job is green.
- Deployment only from a tagged commit, with explicit owner approval for that tag, following `AGENTS.md` ("Deployment"): check the queue, `make backup`, pull by digest, migrate, smoke test, synthetic e2e case, record versions. Never on an agent's own initiative.
- Never put server, host or institution details in the repository.
