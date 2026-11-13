---
description: Review the current changes for security and data-protection problems.
---

# Security review

Scope: $ARGUMENTS

Look for: secrets or credentials in code, config or history; path traversal and unsafe file handling in uploads, downloads and the case store; missing validation (type, size, content); authentication, authorisation, ownership and quota gaps; stack traces or internal paths exposed to users; unsafe container settings for steps (network, root, writable mounts outside `/out` and `/tmp`); executor allow-lists; any patient or non-redistributable data in code, fixtures or tests; public-repository leaks (people's names, host or institution details).

Return prioritised findings with file references. Do not edit files.
