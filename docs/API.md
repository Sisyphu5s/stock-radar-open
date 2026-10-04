# API overview

The FastAPI application mounts its public routes below `/api/v1`.

- `/api/v1/health` — liveness and readiness checks.
- `/api/v1/market` — snapshots, indices, quotes, and market-source status.
- `/api/v1/stocks` — stock details, klines, indicators, financials, and capital data.
- `/api/v1/signals` — signal catalogs, scans, events, and filters.
- `/api/v1/alpha`, `/api/v1/factors`, `/api/v1/experiments` — factor research and evaluation.
- `/api/v1/paper` — paper-trading accounts, orders, fills, and performance.
- `/api/v1/copilot` — optional streaming assistant endpoints.

The running service is the source of truth for request and response schemas. Open `/docs` or `/redoc` after starting the backend. The generated `scripts/backend/contract.json` file is used by CI to detect frontend/backend schema drift.
