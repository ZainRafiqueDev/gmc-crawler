# GMC Compliance Bot

Config-driven Google Merchant Center compliance monitor. Runs fully
end-to-end against seeded mock data today; drop real store/LLM/GMC
credentials into `.env` and it switches to live mode with **no code
changes**.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate        # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements-dev.txt
cp .env.example .env          # leave everything blank for full mock mode
uvicorn app.main:app --reload
```

On boot the first thing printed is the mode banner, so it's never
ambiguous which parts are live vs. mocked:

```
========================================================================
GMC Compliance Bot - startup mode
------------------------------------------------------------------------
  Store:         MOCK  (seeded mock catalog)
  LLM provider:  OLLAMA  (local, no API cost)
  GMC connect:   DISABLED - GMC not configured, skipping auto-connect
  Notifications: LOGGED ONLY (no RESEND_API_KEY/ALERT_EMAIL_TO)
  API auth:      DISABLED - /scan and /dashboard/alerts are open to anyone who can reach this server
========================================================================
```

- `GET /health` - public, no secrets in the response (mode flags only)
- `POST /scan` - trigger a compliance run immediately
- `GET /dashboard/alerts` - alerts raised so far

## Security

- **No secrets ever leave the server.** Store/LLM/GMC/Resend credentials are
  read from env vars, used only as outbound auth (HTTP headers, not URLs),
  and never appear in a response body, log line, or the OpenAPI schema —
  `Settings` is never used as a request/response model. Verified by
  `tests/test_api_auth.py::test_health_response_contains_no_secrets`.
- **`/scan` and `/dashboard/alerts` require `API_AUTH_TOKEN`** (a
  constant-time-compared bearer token) whenever it's set. It's optional in
  pure mock/local dev, but `load_settings()` logs a loud warning at startup
  if a live store or GMC is configured without one — those endpoints can
  return real catalog/violation data and, once GMC is configured, trigger a
  live `claimwebsite` call, so don't expose them un-authed past localhost.
- No CORS middleware is enabled, so a browser page on another origin can't
  call this API at all by default.
- Test fixtures never contain real-looking credentials: the fake GMC
  service-account key used in the E2E tests is generated fresh per test
  session (`tests/conftest.py::fake_service_account_path`), not committed
  to the repo.

## Config

See `.env.example`. Runtime behavior:

| Config present | Behavior |
|---|---|
| Nothing set | Mock catalog, Ollama, report generated, GMC skipped |
| Store only | Real catalog, report generated, GMC still skipped |
| Store + `OPENAI_API_KEY` | Same, GPT instead of Ollama |
| Store + LLM + GMC | Full pipeline, auto-connects only if zero critical violations |

`app/config.py` fails fast at startup (`ConfigError`) on inconsistent
config: `LLM_PROVIDER=openai` with no key, a live `STORE_PLATFORM` with no
`STORE_URL`, partial GMC config, or a malformed `OLLAMA_HOST`.

## Architecture

- `app/connectors/` - `StoreConnector` interface: `mock`, `woocommerce`, `shopify` (stub)
- `app/collector.py` - Data Collector Agent, the only bridge to raw store data
- `app/rules/` - deterministic checks (`deterministic.py`) + `category_rules.py`
  (category -> extra required checks: playhouse safety language, battery
  certification for scooters/motors, appliance disclosures)
- `app/llm/` - `LLMProvider` interface: `ollama`, `openai`, `mock`; `checks.py`
  is the fuzzy-judgment track (misleading claims, deceptive pricing, prohibited category)
- `app/engine.py` - runs both tracks per product, isolates LLM failures per-product
- `app/reporting.py` + `app/notifications/` - severity-tagged alerts, deduped across runs
- `app/gmc/` - GMC client, site-verification injector, and the auto-connect gate
  (zero critical violations required, fails closed)
- `app/policy_watcher/` - hash-diff + LLM change extraction on GMC Help Center pages
- `app/scheduler.py` - daily cron + immediate trigger on policy change, single
  `Pipeline.run_once` concurrency-guarded entry point for both
- `app/pipeline.py` - wires it all together from `Settings`

## Testing

```bash
pytest -v
```

67 tests, zero live network calls (every external HTTP call - store API,
GMC API, Ollama/OpenAI, GMC Help Center pages, Resend - is mocked with
`respx`). Runs in CI on every push via `.github/workflows/ci.yml`.

Highlights:
- `tests/test_gmc_gate.py` - the auto-connect gate: one critical violation
  must never trigger `claimwebsite`, even with full GMC config present
- `tests/test_e2e_pipeline.py` - E2E-1 through E2E-5, one per config state
  from the table above, exercising the real pipeline end to end
- `tests/test_resilience.py` - isolated LLM/store failures, no duplicate
  alerts on repeat runs, fail-fast on malformed config

## Not yet built

- Postgres/Supabase persistence (dashboard/dedup stores are in-memory for now)
- pgvector-backed policy RAG (policy watcher currently diffs raw page hashes)
- Next.js dashboard frontend
- Celery/Redis (not needed at this scale - APScheduler is single-process)
