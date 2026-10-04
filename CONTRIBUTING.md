# Contributing

Thanks for helping improve Stock Radar.

## Before opening a pull request

1. Create a focused branch from `main`.
2. Do not commit `.env` files, databases, caches, downloaded market data, model checkpoints, or credentials.
3. Keep API changes, frontend types, tests, and `scripts/backend/contract.json` in sync.
4. Explain behavior changes and data-source assumptions in the pull request.

## Local checks

```bash
cd frontend
npm ci
npx tsc -b
npm run lint
npm run build
npm audit --audit-level=low

cd ../backend
uv pip sync --python .venv/bin/python requirements.lock
SR_MLX_OFF=1 SR_GP_BACKEND=numpy PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python ../scripts/backend/graph_audit.py
```

The MLX dependency is optional. CPU-only contributors should use `SR_MLX_OFF=1` and the lock file.

## Pull requests

Keep pull requests small enough to review. Include tests for bug fixes and behavior changes. Avoid adding provider data or screenshots containing personal information. Maintainers may request a focused reproduction or a contract update before merging.

To update Python dependencies, edit `backend/requirements.txt` or `backend/requirements-dev.txt`, then regenerate `backend/requirements.lock` with `uv pip compile --universal --python-version 3.12 --output-file backend/requirements.lock backend/requirements-dev.txt`.
