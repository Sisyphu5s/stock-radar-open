# Stock Radar

Stock Radar is an open research workbench for A-share market monitoring, signal exploration, factor research, backtesting, and paper trading.

This public repository is a clean snapshot focused on the application, tests, and contributor workflow. It does not include personal configuration, deployment files, market-data snapshots, databases, runtime caches, or historical private repository data.

## What it includes

- Multi-source market data adapters with cache-aware degradation.
- Signal scanning, watchlists, screeners, stock workbench views, and paper trading.
- Technical indicators, risk metrics, Alpha101 experiments, factor evaluation, and backtesting.
- Symbolic regression and neural factor experiments with NumPy fallback paths.
- React frontend with responsive market and research workspaces.
- FastAPI backend with SQLite persistence, background jobs, SSE progress events, and OpenAPI documentation.

## Architecture

```text
React + Vite frontend
        | /api/v1
        v
FastAPI API and domain services
        |
        +-- market data providers and caches
        +-- SQLite repositories
        +-- research and backtest engines
        +-- background task workers
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the public architecture notes. When the backend is running, interactive API documentation is available at `/docs` and `/redoc`.

## Requirements

- Python 3.12 or newer
- Node.js 22.12 or newer (or Node.js 20.19.x) and npm
- `uv` for Python environment management
- macOS or Linux
- Network access to the configured market-data providers for live data

Apple Silicon users may install the optional MLX dependency for GPU-backed experiments. The base installation uses NumPy-compatible paths and does not require MLX.

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/Sisyphu5s/stock-radar-open.git
cd stock-radar-open
cp backend/.env.example backend/.env
```

Keep `backend/.env` local. Never commit API keys, cookies, database files, downloaded market data, or provider credentials.

### 2. Install backend dependencies

```bash
uv venv --python 3.12 backend/.venv
uv pip sync --python backend/.venv/bin/python backend/requirements.lock
```

For Apple Silicon MLX acceleration, install the optional set after the locked base environment:

```bash
uv pip install --python backend/.venv/bin/python -r backend/requirements-mlx.txt
```

### 3. Install frontend dependencies

```bash
cd frontend
npm ci
cd ..
```

### 4. Start the backend

```bash
cd backend
SR_MLX_OFF=1 SR_GP_BACKEND=numpy PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API listens on `http://127.0.0.1:8000`. Health checks are available under `/api/v1/health`.

### 5. Start the frontend

In a second terminal:

```bash
cd frontend
SR_PROXY_TARGET=http://127.0.0.1:8000 npm run dev
```

Open the URL printed by Vite, normally `http://127.0.0.1:5173`.

### Optional native acceleration

On macOS with Xcode Command Line Tools installed:

```bash
bash scripts/native/build.sh
```

If the native library is unavailable, the application falls back to NumPy implementations.

## Configuration

`backend/.env.example` documents the supported settings:

- `SR_DATA_PROVIDER=auto|akshare|sina|tencent` selects the live data-source strategy. Automatic selection uses the available AkShare, Sina, and Tencent adapters according to the requested data type and source health.
- `SR_GP_BACKEND=auto|mlx|numpy` selects the factor-computation backend.
- `SR_API_TOKEN` optionally protects API routes with `X-API-Token`. The current web client does not supply that header. Keep the development servers on loopback; for network deployments, put the UI and API behind a trusted authentication layer. Exposing Vite also exposes its `/api` proxy.
- Both development servers bind to loopback by default. `SR_FRONTEND_HOST` changes the Vite binding; set `SR_HOST` when starting the backend with `python -m app.main`, or pass `--host` directly to Uvicorn.
- `SR_CORS_ORIGINS` limits cross-origin browser access.
- `SR_LLM_BASE_URL`, `SR_LLM_API_KEY`, and `SR_LLM_MODEL` enable the optional assistant integration.

Live provider access is required for live market pages. Tests use synthetic data and mocks; the application does not promise an offline market-data mode.

## Validation

```bash
# Frontend
cd frontend
npm ci
npx tsc -b
npm run lint
npm run build
npm audit --audit-level=low

# Backend
cd ../backend
SR_MLX_OFF=1 SR_GP_BACKEND=numpy PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m pytest -q
```

The GitHub Actions workflow runs the CPU-safe backend path, frontend checks, dependency audits, a public-release guard, and a native build smoke test on pull requests and pushes to `main`. The Python lock file includes the test dependencies.

## Data, model, and financial-use notice

Market data is fetched at runtime from third-party providers. No market-data snapshots are distributed with this repository. Review each provider's terms, rate limits, and redistribution rules before using the application.

Backtests, factor scores, signals, and paper-trading results are research outputs. They can contain look-ahead, survivorship, data-quality, and execution-model limitations. They are not investment advice and do not guarantee future performance.

The optional LLM assistant can produce incorrect or incomplete output and may incur API costs. Do not send private account information or credentials to an LLM provider.

## Project layout

- `backend/app/` — FastAPI routes, domain services, storage, providers, and research engines.
- `backend/tests/` — unit and integration tests built around synthetic data and mocks.
- `frontend/src/` — React application.
- `scripts/backend/` — API graph and schema checks.
- `scripts/frontend/` — frontend audits and token generation.
- `scripts/native/` — optional native acceleration build.
- `docs/` — public architecture, API, data-source, and design notes.

## Contributing and license

Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Security reports belong in [`SECURITY.md`](SECURITY.md). This project is released under the MIT License; see [`LICENSE`](LICENSE). Third-party credits are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
