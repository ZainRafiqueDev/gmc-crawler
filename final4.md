# GMC Compliance Checker — What's Implemented (current snapshot)

A complete, stand-alone inventory of everything built so far, as of this session. For the
round-by-round narrative see `final.md`; for the original topical reference see `final2.md`;
for a flow-oriented walkthrough see `final3.md`. This file exists because the two most
recent rounds (fixed-size LLM sampling honesty + risk-weighted scaling, and the free-tier
deployment guide) landed after those.

## 1. What this is

A Python/Playwright/LangGraph tool that crawls an e-commerce store (WooCommerce or generic)
and produces a Google Merchant Center (GMC) policy-compliance report: what's confirmed
broken, what's at risk, what couldn't be verified and why, with real policy citations and
severity/impact tiering. Not a general SEO or accessibility scanner — scoped specifically to
GMC Shopping-ads/free-listings compliance.

## 2. Pipeline architecture

LangGraph `StateGraph` (`app/graph.py`), five nodes, flow control only (this project does
**not** use LangChain):

```
detect_platform → crawl_and_classify → deterministic_checks → llm_grading → compile_report
```

A shared `AuditState` TypedDict threads through all five nodes: settings, browser, db,
llm_cache, platform detection result, site map, findings, llm_coverage stats, product
images. `run_audit()` / `run_audit_streaming()` in `app/graph.py` are the two entry points
(CLI and API both call these — no separate logic path per caller).

## 3. Crawling (`app/fetch.py`, `app/site_mapper.py`)

- Playwright-driven, HTTP-status-aware retry/backoff.
- Per-domain politeness throttling (`_DomainThrottle`, `CRAWL_DOMAIN_MIN_DELAY_SECONDS`).
- Anti-bot JS-interstitial detection with a resolution-wait, and cookie-consent-banner
  auto-dismissal, so a crawl doesn't silently stall behind either.
- Honest, specific failure categorization — never a generic "couldn't verify." Every
  unreachable page gets one of: `not_found`, `blocked_ssrf`, `captcha_blocked`,
  `bot_blocked`, `rate_limited`, `network_error`, `http_error`, `unknown`
  (`FAILURE_CATEGORY_LABELS` / `_SHORT_LABELS` / `_RECOMMENDATIONS` in `app/fetch.py`),
  surfaced per-page in the report with a category-specific recommendation, not just "failed."
- `SiteMap.crawl_totally_failed` / `robots_disallowed` — a crawl that got nothing (or was
  refused by robots.txt) is reported as **could not run**, not as a pile of confident
  "missing X" findings. `RiskScore.not_applicable` is the single computed source of truth
  every report section reads, closing a whole class of "some sections show a confident score
  while others show N/A" contradictions.
- `detected_language` per page (from `<html lang>`) so the required-page classifier doesn't
  produce a false "missing" verdict on a page it simply can't read.
- Opt-in BYO residential/rotating-proxy support (`app/proxy_config.py`) — off by default, no
  bundled proxy infrastructure, `contextvars`-based so the SSRF guard stays active even when
  proxied.

## 4. Security (`app/security/`)

- **SSRF guard** (`ssrf_guard.py`): three independent layers — upfront URL validation,
  per-request interception via `SSRFSafeTransport`, and a post-navigation final-URL check
  (catches redirect-based bypass). `ssrf_requests_validated` / `ssrf_requests_blocked` are
  positive, countable confirmation that all three layers actually ran for a page — not just
  an absence of blocked requests — and are correctly populated even on a fetch that ultimately
  fails (both the counter-timing bug and the missing-copy-onto-`CrawledPage` bug that used to
  zero this out on failure were found live against `nike.com` and fixed).
- Rate limiting (`security/rate_limiter.py`) on the API's audit-creation endpoint.

## 5. Deterministic checks (`app/checks/`)

Rule-based checks needing no LLM: required policy pages present, business-identity
(email/phone) consistency across pages, contact-form field completeness, product
image alt-text/broken-image/low-resolution, duplicate product listings, external/mixed-content
link hygiene, WooCommerce/generic product data (price, availability) via platform API when
credentials are given, falling back to page scraping otherwise.

- **Review-form false-positive fix**: WordPress core's comment form uses a sibling
  `<label for="author">`, not a parent `<label>` — `_field_hint()` only walked up to a
  parent, so this form's fields were misclassified as suspension-risk findings. Fixed by
  excluding the whole form via a signature match (`_REVIEW_FORM_HINT_RE`) before any
  field-level classification runs, not by trying to patch the label-lookup itself.
  Live-validated on `vellano.site`.
- 503 responses are categorized as `http_error`/transient, not folded into a generic
  "not found" bucket.

## 6. LLM-graded checks (`app/llm/checks.py`)

Claude or OpenAI (`Settings.llm_provider`), anti-hallucination pattern throughout: forced
tool-use/structured-output schemas requiring a verbatim `evidence_quote` field, never a bare
verdict. A page that passes produces no `Finding` (same convention as deterministic checks).

Four checks:
- `check_policy_page_substance` — does a policy page (privacy/shipping/returns/ToS) actually
  say something substantive, not just exist.
- `check_editorial_quality` — homepage + a sample of product pages.
- `check_prohibited_content` — product pages, including counterfeit/brand-risk screening
  (watch-term guidance: replica, 1:1, mirror quality, AAA-as-quality-grade, knockoff — plus an
  explicit "a brand name alone is not evidence of counterfeiting" rule, live-validated to
  correctly pass a legitimate "compatible with iPhone 14" page and correctly flag a genuine
  "AAA 1:1 Mirror Replica Rolex" page).
- `check_claim_policy_contradiction` — cross-checks a shipping/returns claim on a
  homepage/product page against the store's own policy page text. Cheap regex pre-filter
  before any LLM call; topic-locked (shipping claims only checked against shipping policy,
  etc. — cross-topic checking produced false mismatches in live testing). Two real false
  positives were found live (a claim correctly matching its own policy's normal exceptions
  clause was still flagged) and prompt-hardening alone was **not sufficient** — reproduced on
  5/5 real pages even after adding negative examples. Fixed with a deterministic code-level
  backstop (`_same_day_count_in_both()`) that discards the finding when both quotes name the
  identical day count, regardless of what the model concluded. Re-verified live: 0 false
  positives on two real stores after the fix, a genuine synthetic mismatch still caught.

### Fixed-size sampling honesty + risk-weighted scaling (this session, most recent round)

The problem: `check_editorial_quality`/`check_prohibited_content` always sampled a flat
first-5 product pages by crawl order, regardless of catalog size, with **no disclosure**
anywhere in the report — a 500-product store and a 5-product store got identically-worded
"Pass" language.

- **`LLMCoverageStats`** (`app/models.py`) — `llm_configured`, `total_reachable_product_pages`,
  `product_pages_checked`, computed `coverage_fraction`/`is_partial`. Threaded through
  `run_llm_checks()` → `AuditState` (`app/graph.py`) → `generate_markdown_report()`
  (`app/report.py`) → both CLI and API report-regeneration call sites.
- At a Glance now states, e.g.: *"Prohibited-content / editorial-quality screening: 5 of 45
  product page(s) checked (11%)."* The Policy-by-Policy matrix annotates an otherwise-clean
  sampled area as `Pass (partial coverage: N/M)` instead of a bare, misleadingly-confident
  `Pass`.
- **Live-confirmed** against `meo.fr` (100-page crawl budget, 45 real reachable product pages
  — `britanniagifts.us`/`vellano.site` were both unreachable this session, confirmed
  `network_error` by direct probe before substituting): report showed exactly
  `Prohibited-content / editorial-quality screening: 5 of 45 product page(s) checked (11%)`
  and `Prohibited Content | Pass (partial coverage: 5/45)`.
- **Sampling strategy** — three real-cost options were priced from current gpt-4o-mini
  pricing ($0.150/1M input, $0.600/1M output tokens) and presented via `AskUserQuestion`:
  (A) scale to catalog size capped at 50 (~10x cost on a large catalog), (B) keep the flat-5
  sample but risk-weight which 5 (no cost change), (C) modest scale-up capped at 15 combined
  with risk-weighting (~3x cost, best coverage-per-dollar). **User chose Option C.**
- Implemented: `_product_sample_size(total, cap)` = `min(cap, max(5, ceil(5% of reachable
  product pages)))`. New `Settings.llm_product_sample_cap` (default 15, hard ceiling 50 via
  `HARD_MAX_LLM_PRODUCT_SAMPLE` in `app/config.py`, same clamp-validator pattern as every
  other resource ceiling — `CRAWL_MAX_PAGES` etc). `_select_product_sample()` replaces flat
  first-N-crawled with two cheap signals already available from the crawl (no new brand
  watchlist, no external data fetch): a price that's a **rank-based** bottom-decile outlier
  *within this store's own catalog* (deliberately relative — "cheap" varies by store; rank-
  based specifically because real catalogs have many exact-tie prices, e.g. lots of `$X.99`
  items, which a value-threshold cutoff would over-match), and thin product copy (<40 words).
  Ties keep crawl order for determinism.
- 25 tests (`tests/test_llm_checks.py`) cover the sample-size formula at multiple catalog
  sizes, risk-weighted selection actually surfacing an outlier-priced and a thin-content page
  crawled last (not first), the cap holding at 15 on a 400-product synthetic catalog, and
  coverage stats reflecting the real scaled sample end-to-end. Full suite: **419 passed.**

## 7. RAG policy grounding (`app/llm/policy_rag.py`)

Real GMC Help Center pages, live-scraped, chunked, embedded (OpenAI `text-embedding-3-small`),
cosine-similarity retrieval done in plain Python over a JSON float-array column (deliberately
not a native pgvector column — this corpus is small enough that a hard Postgres+pgvector
dependency isn't needed). `PolicyContext.from_real_index` / `verified_at` (the oldest cited
chunk's scrape timestamp) surfaced per finding as "Official Source: [url] (last verified:
[date])" — live-confirmed showing a real date from this project's own index-build history, not
a placeholder. Falls back to hand-written stub snippets (`policy_snippets.py`) only when
retrieval returns nothing. `app/policy_watcher.py::check_policy_sources` re-checks the real
source pages on an interval, independent of any store's own monitoring schedule.

## 8. Classification layers (post-hoc, non-mutating, both reuse `policy_area_for_finding`)

- **`ImpactTier`** (`app/impact_tier.py`) — `SUSPENSION_RISK` / `LISTING_DISAPPROVAL` /
  `QUALITY_IMPROVEMENT`, grounded per `check_id` against Google's actual enforcement-tier
  policy text, not guessed. Defaults to the cautious `LISTING_DISAPPROVAL` for anything
  ungrounded rather than assuming harmless.
- **`AdsEligibilityImpact`** (`app/ads_eligibility.py`) — `ADS_AND_LISTINGS` / `LISTINGS_ONLY`
  / `UNCLEAR`. Real research this session found Google's "Free listings policies" page mirrors
  the Shopping-ads policy category-for-category, so every currently-grounded check_id maps to
  `ADS_AND_LISTINGS`; `LISTINGS_ONLY` exists as a value with no current check grounded there
  (no evidence found for anything narrower), rather than forcing a false 3-way split.
  Live-confirmed on `vellano.site`: 69 findings classified, Executive Summary correctly stated
  how many would also affect paid ads eligibility.

## 9. Report generation (`app/report.py`, `report_docx.py`, `report_pdf.py`)

Markdown is the source of truth; docx/pdf reuse the same parsing. Sections: "This Audit Could
Not Run" banner (when applicable, replacing everything else), Executive Summary/At-a-Glance,
Suspension Risk Findings (rich format with policy requirement + official source + freshness +
ads-eligibility line), confidence-aware Policy-by-Policy matrix (`Fail` ≥1 CONFIRMED / `At
Risk` only POTENTIAL_RISK / `Cannot Verify` only CANNOT_VERIFY / `Pass`, now annotated with
partial-coverage where relevant), Other Findings, Page-by-Page, Required Fixes, Final
Assessment. Score-floor and coverage/exclusion transparency built directly into the breakdown
so a low-confidence audit can't render as if it were a confident one.

## 10. Monitoring / scheduling (`app/monitor_service.py`, `app/scheduling.py`)

`MonitorService` ties together store registry, cheap content+DOM-hash change detection
(`app/change_detection.py`), the full audit pipeline, delta-report generation, and a
swappable `SchedulerBackend` (currently `APSchedulerBackend`, single-process — the seam is
there for Celery/RQ later). Modes: `interval`, `on_change`, `both`. `app/policy_watcher.py`
runs independently of any store's schedule.

## 11. Frontend / API

FastAPI backend (`app/api/main.py`) — four endpoints matching a Next.js frontend's four
screens, deliberately minimal, not a general-purpose API. Next.js 16 / React 19 frontend
(`frontend/`). CORS origin, rate limiting, and job persistence (`AuditJobRecord`,
`AuditRun`, both with retention pruning) all configurable via `Settings`.

## 12. Deployment (this session, most recent addition)

Live-researched (not from training knowledge — free-tier terms change often enough that a
stale claim here would actively mislead) current 2026 terms for Render, Supabase, Vercel, and
Railway before writing anything.

- **`Dockerfile`** (repo root) — based on Playwright's own maintained image
  (`mcr.microsoft.com/playwright/python`) so Chromium's system dependencies are present;
  Render's native Python buildpack doesn't have them.
- **`.dockerignore`** — excludes `frontend/`, `.venv/`, `.git/`, local `*.db` files, etc.
- **`DEPLOYMENT.md`** — full walkthrough: Supabase (free Postgres, no pgvector extension
  needed — confirmed by re-reading `app/db.py`'s own comment, not assumed), Render free Web
  Service + Docker (backend), Vercel Hobby (frontend), env-var wiring both directions, and a
  cost table.
  - **Honest constraint called out explicitly, not glossed over**: this app's scheduled
    store-monitoring feature needs a continuously-running process, which is fundamentally in
    tension with free-tier behavior — Render's free Web Service sleeps after 15 minutes idle,
    and Supabase's free Postgres project pauses after 7 days with zero DB activity. Documented
    two honest ways to live with it (an external free uptime-ping keep-alive, or accepting
    on-demand-audits-only and skipping scheduled monitoring) rather than presenting a fully-
    free stack as if it transparently supports always-on scheduling.
  - Railway explicitly evaluated and ruled out, with the reason stated: no real permanent free
    tier as of 2026 (30-day $5 trial, then $1/month credit — not enough for an always-on
    Playwright container).
  - Neither Anthropic's nor OpenAI's API has a free tier — the guide recommends standardizing
    on `LLM_PROVIDER=openai` + `gpt-4o-mini` for a cost-sensitive deployment (an explicit
    departure from the repo's own Claude default) since `OPENAI_API_KEY` is required
    regardless for the RAG embeddings, and gpt-4o-mini is roughly an order of magnitude
    cheaper per call than Claude Sonnet.

## 13. Testing

`pytest` + `pytest-asyncio`. 419 tests passing as of this session, full suite green. Fake-LLM-
client pattern (`FakeClaudeClient` + `monkeypatch.setattr("app.llm.checks.get_llm_client", ...)`)
isolates every LLM-graded check test from real API calls. Live validation is a separate,
deliberate discipline on top of this — ad-hoc scripts run against real, currently-reachable
stores (this session: `meo.fr`), with raw output actually read, not assumed — because unit
tests alone were never treated as sufficient evidence for the kind of claims this project
makes (e.g., "the report now shows an honest coverage percentage").

## 14. Explicitly not built yet (open, not forgotten)

- **Purchase-journey reachability validation** (`app/checks/purchase_journey.py` exists and is
  wired into the CLI behind `--enable-purchase-journey --confirm-test-payment-mode`, never
  submits payment) — the live-store validation step is blocked on the user naming a real
  store, ideally with a sandbox/test payment mode. Not yet provided.
- **Adaptive page-budget scaling + per-category page caps + failure-reporting specificity
  audit** — the newest follow-up spec, not yet started. Explicit instruction from the user:
  don't let the failure-reporting audit (Part 3) get skipped in favor of the more visible
  budget/cap work (Parts 1-2).
- **Annotated screenshots for suspension-risk findings**, and **audit-history browsing UI +
  policy-change-triggered re-audits** — both scoped and confirmed via `AskUserQuestion` in an
  earlier round (skip screenshots for aggregate findings; re-audit every `on_policy_change`
  store, not just ones with past findings in that area) but not yet implemented — deprioritized
  behind the two newer follow-up rounds.
