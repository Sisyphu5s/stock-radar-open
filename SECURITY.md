# Security policy

## Supported versions

The `main` branch is the actively maintained version. Development snapshots may change without notice.

## Reporting a vulnerability

Please use a private GitHub Security Advisory for this repository. Do not open a public issue containing credentials, tokens, webhook secrets, personal data, or an exploit that affects a deployed instance.

Include the affected path, reproduction steps, impact, and a safe way to contact you. Redact all secret values from the report.

## Local secret handling

- Keep `backend/.env` outside commits.
- Use short-lived, least-privilege provider credentials.
- Rotate a credential immediately if it appears in a log, archive, issue, pull request, or Git history.
- Do not upload SQLite files, downloaded provider responses, model checkpoints, or session exports.
