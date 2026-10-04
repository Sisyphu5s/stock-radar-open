# Stock Radar frontend

The frontend is a React 19 + TypeScript + Vite application for the market radar and research workbench.

## Local development

```bash
npm ci
SR_PROXY_TARGET=http://127.0.0.1:8000 npm run dev
```

The Vite development server binds to loopback by default and proxies `/api` requests to the backend. Override `SR_FRONTEND_PORT`, `SR_FRONTEND_HOST`, or `SR_PROXY_TARGET` when needed.

## Checks

```bash
npx tsc -b
npm run lint
npm run build
node ../scripts/frontend/graph-audit/graph-audit.mjs
node ../scripts/frontend/time-moment-check.mjs
node ../scripts/frontend/design-system-audit.mjs
```

The design contract used by the audits is documented in [`../docs/DESIGN-CONTRACT.md`](../docs/DESIGN-CONTRACT.md).
