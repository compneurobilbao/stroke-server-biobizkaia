#!/usr/bin/env sh
set -eu
ENV_FILE_RE='(^|/)\.env(\.[^/]*)?$'
if git ls-files | grep -E "$ENV_FILE_RE" | grep -v '\.example$' >/dev/null; then
  echo "Do not track .env files (only *.example templates)"
  exit 1
fi
if git diff --cached --name-only --diff-filter=ACMR | grep -E "$ENV_FILE_RE" | grep -v '\.example$' >/dev/null; then
  echo "Do not commit .env files (only *.example templates)"
  exit 1
fi
if git ls-files | grep -E '^(backups/|data/pre-restore/|data/studies/.+)' | grep -v '^data/studies/\.gitkeep$' >/dev/null; then
  echo "Do not track backups or study data"
  exit 1
fi
if git diff --cached --name-only | grep -E '^(backups/|data/pre-restore/|data/studies/.+)' | grep -v '^data/studies/\.gitkeep$' >/dev/null; then
  echo "Do not commit backups or study data"
  exit 1
fi
if grep -r "BEGIN RSA PRIVATE KEY\|AWS_SECRET_ACCESS_KEY\|password *= *['\"][^'\"]" \
  --exclude-dir=.git \
  --exclude-dir=node_modules \
  --exclude-dir=.venv \
  --exclude-dir=dist \
  --exclude-dir=coverage \
  --exclude-dir=studies \
  --exclude-dir=pre-restore \
  --exclude-dir=backups \
  --exclude=.env \
  --exclude=check-no-secrets.sh \
  .; then
  echo "Potential secret found"
  exit 1
fi
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks dir --no-banner --redact --config .gitleaks.toml .
else
  echo "gitleaks not installed: skipped (it runs in pre-commit and CI)"
fi
echo "Secret check OK"
