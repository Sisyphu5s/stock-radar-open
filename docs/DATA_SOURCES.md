# Data sources and usage boundaries

Stock Radar fetches market data at runtime. It does not distribute market-data snapshots, databases, cookies, or provider credentials.

## Providers

- **AkShare** is the primary adapter for many A-share datasets.
- **Sina** and **Tencent** adapters provide selected quote and fallback paths.
- Optional LLM providers are configured through an OpenAI-compatible base URL and API key.

Provider availability, field definitions, rate limits, and redistribution rights can change. Users are responsible for reviewing the current terms of each upstream service and for keeping request volume within those limits. A provider response must not be treated as a license to redistribute the returned data.

## Runtime behavior

`SR_DATA_PROVIDER=auto` enables the configured provider strategy and cache-aware fallback. A live network connection is required for live market pages. Tests use synthetic fixtures and mocks; no mock market-data provider is promised by the application runtime.

## Credentials

Put credentials only in a local ignored `backend/.env` file or an environment manager. Never commit API keys, cookies, webhook secrets, or downloaded data.
