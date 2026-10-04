# Architecture

Stock Radar is split into a browser client, an HTTP API, domain services, persistence, and background work.

```text
frontend/src
  ├── pages and domain components
  ├── data/query and API clients
  └── theme and shared utilities
        │
        ▼
backend/app
  ├── api              HTTP routes and schemas
  ├── core             runtime, jobs, scanning, domain orchestration
  ├── lib              indicators, factors, backtests, risk, signals
  └── storage          SQLite models, repositories, caches, providers
```

## Boundaries

- API routes validate requests and serialize responses; domain behavior belongs in `core` or `lib`.
- Storage owns persistence and provider adapters. Network failures should degrade through cache or an explicit error response.
- Long-running research work runs through the background task system and reports progress through the API/SSE endpoints.
- The frontend treats server state as query data and keeps local stores for UI state and user preferences.
- Factor research and live signals meet through published factor definitions; experiments do not silently become live signals.

## Runtime resources

SQLite files, caches, model checkpoints, native binaries, and local environment files are runtime artifacts. They are intentionally excluded from version control. The application can use NumPy fallbacks when MLX or the optional native library is unavailable.

## API

The API is mounted under `/api/v1`. Start the backend and use FastAPI's `/docs` or `/redoc` pages for the live contract. `scripts/backend/schema_dump.py` and `scripts/backend/contract.json` provide a machine-readable schema snapshot used by the frontend audit.
