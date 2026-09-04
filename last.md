# GMC Compliance Checker — Everything Implemented, From Scratch

A complete, stand-alone inventory of the whole system as it exists right now. Earlier snapshots
(`final.md` — round-by-round chronicle, `final2.md`/`final4.md` — earlier topical snapshots,
`final3.md` — flow walkthrough) cover the history up to their own point in time; this file is the
current, full picture including the newest work: adaptive crawl budgeting, audit history,
policy-change re-audits, annotated screenshots, a real production deployment (with every real bug
hit along the way fixed, not just documented), and live purchase-journey validation against a real
checkout.

## 1. What this is

A Python/Playwright/LangGraph tool that crawls an e-commerce store (WooCommerce or generic) and
produces a Google Merchant Center (GMC) policy-compliance report: what's confirmed broken, what's
at risk, what couldn't be verified and why, with real policy citations and severity/impact
tiering. Scoped specifically to GMC Shopping-ads/free-listings compliance, not a general SEO or
accessibility scanner.

## 2. Pipeline architecture

A LangGraph `StateGraph` (`app/graph.py`), five nodes, flow control only — this project does
**not** use LangChain:

```
detect_platform → crawl_and_classify → deterministic_checks → llm_grading → compile_report
```

A shared `AuditState` TypedDict threads through all five nodes. `run_audit()` /
`run_audit_streaming()` are the two entry points (CLI and API both call these).

## 3. Crawling (`app/fetch.py`, `app/site_mapper.py`)

- Playwright-driven, HTTP-status-aware retry/backoff; per-domain politeness throttling; anti-bot
  JS-interstitial detection with a resolution-wait; cookie-consent auto-dismissal.
- Honest, specific failure categorization, never a generic "couldn't verify" — every unreachable
  page gets one of `not_found`, `blocked_ssrf`, `captcha_blocked`, `bot_blocked`, `rate_limited`,
  `network_error`, `http_error`, `unknown` (`app/fetch.py`'s `FAILURE_CATEGORY_LABELS` /
  `_SHORT_LABELS` / `_RECOMMENDATIONS`), each with its own actionable recommendation, surfaced
  per-page in the report.
- `SiteMap.crawl_totally_failed` / `robots_disallowed` — a crawl that got nothing (or was refused
  by robots.txt) is reported as **could not run**, never as a pile of confident "missing X"
  findings. `RiskScore.not_applicable` is the single source of truth every report section reads.
- Opt-in BYO proxy support (`app/proxy_config.py`), off by default.

### Adaptive page-budget scaling (this round, new)

The flat 150-page default was too small for a large catalog and wastefully large for a small one.
`app/site_mapper.py::_adaptive_page_budget()` now sizes the actual crawl budget from a real
signal before the crawl starts: WooCommerce's own reported product count via a lightweight
`X-WP-Total` header probe (`app/checks/woocommerce_products.py::fetch_wc_product_count`, one
`per_page=1` request, not a full catalog fetch) when credentials are configured — the most
precise signal — else the sitemap's own catalog-tagged URL count, else the sitemap's total URL
count, else the configured default when there's no signal at all. Formula:
`min(HARD_MAX_PAGES=500, max(floor=60, catalog_signal × 2 + overhead=40))`. Only applied when the
caller didn't pass an explicit `--max-pages`/API override (`Settings.crawl_max_pages_explicit`) —
the setting stays a true override, not just a default. The WC-count probe and the sitemap fetch
run concurrently so the more precise signal doesn't add its own round-trip. 8 tests (pure formula
+ full wiring with a mocked crawl) confirm the scaling, the explicit-override gate, and that
WooCommerce's count wins over the sitemap-derived one when both are available.

### Per-category page caps (this round, new)

Previously one flat "N catalog pages total" cap meant a many-category store dumped its entire
catalog budget into the first few categories discovered. Now `Settings.crawl_max_product_pages_per_category`
(default 30, hard ceiling 200) caps how many product-looking URLs get enqueued **per category**,
while every category-listing page itself is always crawled regardless (cheap, structurally
important). Category attribution prefers real crawl-graph context — a product URL discovered via
a COLLECTION page's own links is keyed to that collection's URL — and falls back to the product
URL's own parent path segment for sitemap-seeded URLs with no discovering page. 3 tests confirm a
synthetic many-category store gets even per-category representation (10 products from each of 3
categories, not 30 from the first one found) and that all category pages get crawled regardless.

### Failure-reporting specificity audit (this round, new — real bugs found and fixed)

A full sweep of every place in the codebase that can produce a `CANNOT_VERIFY` finding or render
failure language, confirming each states a specific category with a specific recommendation, never
a generic "could not verify." Found and fixed **three real gaps**, all the same underlying
pattern: a handful of places made their own direct `httpx` calls outside the main `PageFetcher`
retry path (image probing, form-action reachability probing) and had each independently grown a
bare `except httpx.HTTPError as exc: ...str(exc)...` handler with one guessed recommendation that
didn't fit every cause:

- `app/checks/deterministic.py::check_broken_images` — a network-level probe failure showed the
  raw exception text with a blanket "the image host may be blocking automated requests"
  recommendation, wrong for a plain timeout.
- `app/checks/product_images.py::_probe_image` — the same pattern, plus conflated a genuine
  network failure with a successfully-fetched-but-undecodable/oversized image under one identical
  "may be blocking" message, even though the latter needs completely different advice ("open the
  URL directly, it's not a valid image file").
- `app/checks/form_checks.py::_check_action_reachable` — the more serious of the three: a
  `DNSResolutionError` (a resolver hiccup, not a security decision) fell through to the
  `except SSRFBlockedError` branch (its own subclass relationship) and was reported as
  **CONFIRMED** "form submits to a blocked/non-public address" — reproducing, on a form's action
  URL, the exact false-positive class already found and fixed once for page fetches in
  `app/fetch.py` (a live DNS blip on `britanniagifts.us` mid-crawl, documented in `final.md`).

Fixed by adding one shared `app.fetch.classify_httpx_exception()` used by all three call sites
instead of three independent reimplementations, and by catching `DNSResolutionError` ahead of
`SSRFBlockedError` in `form_checks.py` (matching the pattern `app/fetch.py` already used). 9 new
regression tests confirm each fix live (a DNS hiccup no longer reads as confirmed, a real SSRF
block still does, a plain timeout gets an accurate "network error" reason and recommendation
instead of the old guess). Also confirmed: neither Part 1.1 nor Part 1.2's new code produces any
new failure-language path at all — a per-category-capped product URL is simply never enqueued
(same as today's page-budget truncation already does), never rendered as a failure. One
adjacent, lower-severity gap was found and deliberately **not** fixed, flagged instead: LLM-call
failures (`"The LLM API call failed or returned no usable result"`) discard the real exception
reason that's already captured in the log, always rendering the same generic sentence regardless
of cause (rate limit, auth, timeout, malformed response) — out of the explicit scope (the named
failure categories are crawl-specific) and would need a bigger `LLMClient` Protocol change with a
real concurrency-safety consideration (a shared client instance serves concurrent calls, so a
naive `self.last_error` attribute would race) — surfaced for a future round rather than
rushed.

## 4. Security (`app/security/`)

- **SSRF guard** (`ssrf_guard.py`): three independent layers — upfront validation, per-request
  interception, and a post-navigation final-URL check. `DNSResolutionError` (a reliability
  failure) is deliberately distinct from `SSRFBlockedError` (a security decision) everywhere that
  distinction matters, including — as of this round — the form-action reachability probe.
- Rate limiting on the API's audit-creation endpoint.

## 5. Deterministic checks (`app/checks/`)

Rule-based checks needing no LLM: required policy pages, business-identity consistency across
pages, contact-form completeness, product image alt-text/broken/low-resolution, duplicate
listings, external/mixed-content links, WooCommerce/generic product data. Notable fixed bug from
an earlier round: WordPress's comment form (sibling `<label>`, not parent) was misclassified as a
suspension-risk finding — fixed by excluding the whole form via signature match before field-level
classification runs.

## 6. LLM-graded checks (`app/llm/checks.py`)

Claude or OpenAI, anti-hallucination pattern throughout: forced tool-use/structured-output schemas
requiring a verbatim `evidence_quote`, never a bare verdict. Four checks: `check_policy_page_substance`,
`check_editorial_quality`, `check_prohibited_content` (counterfeit/brand-risk screening, careful
not to flag a brand name alone), `check_claim_policy_contradiction` (a real deterministic backstop
added after live testing found the model conflating "different wording" with "different meaning"
on identical day-count claims).

### Fixed-size sampling honesty + risk-weighted scaling (earlier round this session)

Editorial-quality/prohibited-content checks always sampled a flat first-5 product pages by crawl
order regardless of catalog size, with no disclosure. `LLMCoverageStats` now threads real coverage
numbers into the report ("5 of 45 product page(s) checked (11%)", `Pass (partial coverage: 5/45)`
in the policy matrix). Sampling itself now scales — `min(cap=15, max(5, 5% of catalog))` — and is
risk-weighted (a rank-based bottom-decile price outlier within the store's *own* catalog, plus
thin product copy) rather than always the first 5 crawled, per the user's chosen cost/coverage
option (Option C, priced from real gpt-4o-mini rates before the decision was made).

## 7. RAG policy grounding (`app/llm/policy_rag.py`)

Real GMC Help Center pages, live-scraped/chunked/embedded, cosine similarity in plain Python (no
pgvector dependency needed at this corpus size). Citation freshness (`verified_at`) surfaced per
finding as "Official Source: [url] (last verified: [date])." Falls back to hand-written stub
snippets only when retrieval returns nothing. `app/policy_watcher.py::check_policy_sources`
re-checks real source pages on an interval, independent of any store's own schedule — and, as of
this round, is the trigger for policy-change re-audits (§9 below).

## 8. Classification layers (post-hoc, non-mutating)

- **`ImpactTier`** (`app/impact_tier.py`) — `SUSPENSION_RISK` / `LISTING_DISAPPROVAL` /
  `QUALITY_IMPROVEMENT`, grounded per `check_id` against real policy text.
- **`AdsEligibilityImpact`** (`app/ads_eligibility.py`) — every currently-grounded check maps to
  `ADS_AND_LISTINGS` (real research found Google's free-listings policy mirrors the paid-ads
  policy category-for-category); `LISTINGS_ONLY` exists as a value with no check grounded there
  yet, rather than a forced false split.

## 9. Audit history + policy-change-triggered re-audits (this round, new)

**Audit history** (`app/db.py`, `app/monitor_service.py`, `app/api/main.py`): every retained
`AuditRun` for a monitored store is now browsable. `AuditRun` gained two new columns,
`delta_markdown`/`delta_markdown_major_only` — the delta report was already being *computed* at
run time and written to disk, but silently discarded rather than persisted onto the row; now it's
kept, so any retained historical run (not just the most recent) can show what changed versus its
own predecessor. `GET /api/monitor/stores/{id}/runs` (list, newest first — inherently reflects the
real retention window since old rows are already pruned, nothing extra needed) and
`GET /api/monitor/stores/{id}/runs/{run_id}` (one run's full report + its delta). Frontend: the
Store Report screen (`frontend/app/monitor/[storeId]/page.tsx`) gained a full history list below
the latest report — click an entry to open its own real report, with a delta toggle when one's
available, and a "back to latest" affordance. Both new DB columns auto-migrate onto an existing
`gmc_monitor.db` via the project's already-existing `_add_missing_columns` mechanism — confirmed
with a dedicated regression test that hand-builds an old-schema DB, real pre-existing rows
included, and verifies both new columns backfill correctly and the old data survives untouched.

**Policy-change-triggered re-audits**: `MonitoredStore.on_policy_change` (new boolean column,
independent of and combinable with the existing `mode` field — a store can be `interval` *and*
opted into policy-change re-audits at once). When `policy_watcher.check_policy_sources` detects a
real, non-baseline change to a tracked policy area, **every** store registered for
`on_policy_change` gets a full re-audit — deliberately not just stores with past findings in that
area, since a newly added requirement can affect a store that was previously clean there (decided
explicitly, not assumed). Each triggered run is tagged `trigger="policy_change:<policy_id>[,<policy_id>...]"`,
visible and human-readable ("Policy change (shipping policy)") in the history view. One store's
re-audit failure doesn't block the rest (isolated per-store, logged, continues).

**Live-validated end-to-end** against a real store (`snocks.com`): registered with
`on_policy_change=True`, ran two real full audits (second one produced a real delta — "New issues:
2, Resolved issues: 13" — from real crawl differences), then simulated a real policy-source change
and confirmed a third **real, fresh** audit fired automatically, tagged
`policy_change:shipping_policy`, and appeared correctly in history — not a cache invalidation, an
actual new crawl+grade+report cycle.

## 10. Annotated screenshots for Suspension Risk Findings (this round, new)

`app/checks/screenshot_annotator.py`. Scope (already confirmed in an earlier round): Suspension
Risk Findings only — a site-wide aggregate finding (e.g. a business-identity inconsistency
spanning several pages) has no single element to highlight and is skipped entirely. In practice
this means only LLM-graded findings (`check_id` starting `llm_`, the image-vision check excluded
since it has no text quote to search for) are ever attempted — deterministic findings already
carry a real CSS selector in `Finding.location` and never needed this.

- **Anchoring**: never asks the model to invent a selector. DOM-text-search (a Playwright
  `page.evaluate` walking the live DOM, whitespace/case-normalized) for the finding's own
  already-verified, schema-required `evidence_quote`. A composite finding (claim-vs-policy
  contradiction embeds two quotes in one templated sentence) has its quoted substrings tried
  first, the whole string as a last resort. If nothing matches, the finding is skipped — never a
  guessed location.
- **Capture**: a lightweight *second* visit per distinct page URL, not inline during the original
  crawl — confirmed necessary because `PageFetcher` closes its browser context after every single
  fetch, so no page object survives the crawl to screenshot later. Highlights the matched element
  (outline + overlay), scrolls it into view, screenshots a cropped region with a 40px margin,
  resizes/compresses to JPEG before saving. Multiple eligible findings on the same page share one
  visit.
- **Storage**: `Finding.screenshot_path` (new field, same non-mutating post-hoc-assignment pattern
  as `impact_tier`/`ads_eligibility_impact`), stored relative to `Settings.report_output_dir`.
  Rendered in Markdown as a plain image reference under the finding's Evidence line, and embedded
  natively in both `.docx` (`doc.add_picture`) and `.pdf` (reportlab `Image` flowable) exports —
  both gained a `base_dir` parameter to resolve the relative path; both fall back to a clear
  "(image not available)" text line rather than breaking the export if the file's missing.
- **Testing**: 17 tests on the annotator itself (eligibility rules, quote-candidate extraction,
  slug-safety, and a fully mocked-Playwright integration suite covering the found/not-found/
  navigation-failure/multi-finding-one-visit/different-pages-separate-visits paths) plus 6 more on
  the docx/pdf embedding (real picture embedded when the file exists, graceful text fallback when
  it doesn't or no `base_dir` was given).
- **Live validation status, reported honestly**: the real (non-mocked) components were each
  confirmed live — a real OpenAI call produced a genuine CRITICAL suspension-risk finding from
  real content, and separately, on a real `meo.fr` policy page, the mechanism correctly declined
  to fabricate a screenshot for a real finding whose evidence was a reasoning description rather
  than a literal page quote (the designed safety behavior, working as intended). Across 8 real
  stores tried this session (`meo.fr`'s real product catalog and policy pages, `snocks.com`,
  `britanniagifts.us`, `vellano.site`, `myonlinefashionstore.com`, `meowmeowtweet.com`,
  `blume.com`, `faguo-store.com`), every real suspension-risk hit found was an *absence*-type
  finding ("this required content is missing") — which structurally has no literal quote to
  highlight, by definition, not a bug. No real store produced the *presence*-type finding
  (flagged/contradictory text actually on the page) this mechanism needs to have something to
  screenshot. Accepted as-is per an explicit decision, rather than continuing an open-ended
  external search: the mechanism ships fully built and tested; a live embedded-screenshot example
  from a real store remains open for whenever one naturally occurs (or the user provides one).

## 11. Report generation (`app/report.py`, `report_docx.py`, `report_pdf.py`)

Markdown is the source of truth; docx/pdf reuse the same parsing (not general Markdown parsers —
handle exactly the subset this project produces, including, as of this round, the one image-line
shape). Sections: "This Audit Could Not Run" banner, Executive Summary/At-a-Glance, Suspension
Risk Findings (rich format — policy requirement, official source with freshness, ads-eligibility
impact, and now an optional screenshot), confidence-aware Policy-by-Policy matrix, Other Findings,
Page-by-Page, Required Fixes, Final Assessment.

## 12. Monitoring / scheduling (`app/monitor_service.py`, `app/scheduling.py`)

`MonitorService` ties together the store registry, cheap content+DOM-hash change detection, the
full audit pipeline, delta-report generation (now persisted, not discarded — see §9), and a
swappable `SchedulerBackend` (`APSchedulerBackend` today, the seam is there for Celery/RQ later).
Modes: `interval`, `on_change`, `both`, plus the new independent `on_policy_change` flag.

## 13. Frontend / API

FastAPI backend (`app/api/main.py`), deliberately minimal, matching the Next.js frontend's actual
screens. This round added: `on_policy_change` on registration/response schemas, the audit-history
list/detail endpoints, and `base_dir`-aware docx/pdf downloads (both the ad-hoc-job and
monitored-store download routes) so an embedded screenshot resolves correctly regardless of which
download path served the report.

## 14. Deployment — guide, then a real production deployment, live

`Dockerfile` (Playwright's own maintained base image so Chromium's system deps are present),
`.dockerignore`, and `DEPLOYMENT.md` — a full free-tier walkthrough, live-researched against
current 2026 provider terms rather than assumed. Recommended stack: Vercel (frontend) + Render
free Web Service/Docker (backend) + **Neon** free serverless Postgres (database) — switched from
an initial Supabase recommendation at the user's request; Neon's free tier auto-suspends compute
after 5 minutes idle but auto-resumes on the next query with no manual "unpause" step (unlike
Supabase's multi-day hard pause), a meaningfully better fit for this app's occasional scheduled
queries. Explicitly flagged rather than glossed over: this app's scheduled-monitoring features
(including `on_policy_change` re-audits) need a continuously-running process, which free-tier
hosting (Render sleeps after 15 min idle) can't reliably guarantee — documented as a real
constraint with two honest ways to live with it, not papered over. Render's paid Starter tier
($7/mo) was specifically flagged as **not** the fix for a memory problem (still 512MB, same as
free — only Standard at $25/mo adds real headroom), so as not to recommend spending money on the
wrong upgrade.

**Then this guide was actually followed, live, end-to-end, deploying to Render + Neon + Vercel for
real** — and every real error hit along the way was root-caused and fixed, not just narrated:

- **`ModuleNotFoundError: No module named 'psycopg2'`** — `DATABASE_URL` had Neon's own
  `postgresql://` prefix, not `postgresql+asyncpg://` (this project only installs the async
  driver). Fixed by correcting the env var, not by installing psycopg2.
- **`TypeError: connect() got an unexpected keyword argument 'sslmode'`** — Neon's default
  connection string appends `?sslmode=require` (a `psycopg2`-style param name); `asyncpg` wants
  `?ssl=require` instead. Fixed by correcting the query param.
- **CORS blocked from the deployed Vercel frontend** — `API_CORS_ORIGIN` had a trailing slash;
  this app's CORS check is an exact string match against the browser's `Origin` header, which
  never has one. Fixed by removing it.
- **A real, live-discovered code bug**: once CORS and the DB connection were both fixed, every
  `POST /api/audits` still failed with
  `asyncpg.exceptions.DataError: ... can't subtract offset-naive and offset-aware datetimes`.
  Root cause: every `Mapped[datetime]` column in `app/db.py` used SQLAlchemy's default type
  mapping, which creates a Postgres `TIMESTAMP WITHOUT TIME ZONE` column — but every datetime this
  app actually produces (`_utcnow()`) is timezone-*aware* UTC. SQLite (local dev) doesn't enforce
  this distinction at all, so the mismatch was completely invisible until it hit a real Postgres
  database. Fixed by declaring all 11 datetime columns `DateTime(timezone=True)` explicitly — a
  real, previously-undiscovered portability bug, not a deployment-config issue, caught only because
  the deployment was actually carried through live rather than stopping at "the guide should work."
- **The audit job was silently killed mid-crawl** (`Task was destroyed but it is pending!` followed
  by an unannounced process restart, no Python traceback at all) on Render's free 512MB instance.
  Root-caused as Chromium running out of `/dev/shm` (Docker's default is a fixed 64MB, far below
  what Chromium wants) compounding real memory pressure from a real crawl. Fixed by launching
  Chromium with `--disable-dev-shm-usage` (makes it use `/tmp` instead) across all three real
  launch sites (`app/api/main.py`, `audit.py`, `monitor.py`) — a well-known, standard fix for
  exactly this class of problem in constrained containers, applied only after confirming via the
  real Render logs that this was actually the cause, not a guess.

## 14b. Live purchase-journey validation against a real checkout (this round, new)

Purchase-journey checking (`app/checks/purchase_journey.py`) had never been validated against a
real live checkout — previously blocked on a real external store to test against. Rather than wait
indefinitely, built a fully self-contained, disposable test environment instead:
`validation/purchase_journey_test_store/` — a real WordPress + WooCommerce store via Docker
(`docker-compose.yml` + a scripted `setup.sh`: installs WooCommerce, creates one real test
product, enables WooCommerce's built-in Cash on Delivery gateway as the sole active payment
method), exposed via a real ngrok HTTPS tunnel. The SSRF guard was deliberately left untouched —
the tunnel exists specifically so real-store-shaped traffic reaches the guard normally, not to
route around it.

**Live-validated end-to-end, with real evidence from the actual run, not a reference back to unit
tests**: manually confirmed add-to-cart → checkout in a real browser first (correct product,
price, Cash on Delivery pre-selected), then ran the real `audit.py` CLI with
`--enable-purchase-journey --confirm-test-payment-mode` and `LLM_PROVIDER=openai` against the
tunnel. The generated report's real action log:
`navigate_to_product → read_product_price (24.99, matches the real product) →
click_add_to_cart (button.single_add_to_cart_button) → load_cart_page (/cart/) →
load_checkout_page (/checkout/) → stopped_before_payment` — zero clicks after add-to-cart, no
payment-related action anywhere, proving the structural no-payment-click property from this
specific run's real evidence. Real findings also came out of it: *"No shipping charge shown on
checkout page"* (correct — no shipping method was configured on this minimal test store) and
*"Could not find a price on the cart page"* (an honest `CANNOT_VERIFY`, not a crash).

**Two more real bugs found and fixed** during this validation, both with regression tests:

- `purchase_journey.py`'s very first step (navigating to the product page) had no `try/except`
  around it, unlike every other step in the same flow — a real navigation failure (ngrok
  connection instability under concurrent load) crashed the entire `audit.py` process instead of
  degrading to a `CANNOT_VERIFY` finding like the rest of the check already does.
- The new opt-in `Settings.crawl_extra_headers` (added to let the crawler send
  `ngrok-skip-browser-warning: true` past ngrok's free-tier anti-abuse interstitial — a small,
  generic, off-by-default capability, unrelated to and never a substitute for the SSRF guard, only
  ever adding a header to a request the guard already allowed) crashed settings loading entirely
  on its own documented "off" value (`""`): pydantic-settings auto-JSON-decodes a dict-typed env
  var *before* any field validator gets a chance to run, and raises on an empty string rather than
  treating it as empty. Fixed by declaring the field as a plain string, parsed tolerantly via a
  property instead of relying on automatic dict-type decoding.

`validation/purchase_journey_test_store/README.md` documents the whole workflow (including the
ngrok-interstitial reasoning above and a real Windows-Git-Bash gotcha found live — a leading-slash
`wp-cli` argument silently mangled into a Windows path, corrupting the test store's own permalink
structure) so this environment can be spun back up in one command, without ever depending on an
external live store again. Torn down cleanly after validation (Docker containers + ngrok process
both stopped).

## 15. Testing

`pytest` + `pytest-asyncio`. **477 tests passing**, full suite green. Live validation is a
separate, deliberate discipline on top of unit tests — ad-hoc scripts run against real,
currently-reachable stores with raw output actually read, not assumed, because this project has
never treated passing unit tests alone as sufficient evidence for a live-behavior claim.

## 17. Closing the two remaining verification gaps (live, this round)

Both items left open in §16 were closed this round by running the real thing, not by trusting the
mocked tests — and both live runs surfaced real bugs the mocks had missed, fixed immediately per
the same discipline as every other round.

**Part 1 — adaptive budget + per-category caps, live:**

- `myonlinefashionstore.com` (small Shopify store, no `--max-pages` override): sitemap signal
  found 195 URLs → adaptive budget scaled `150 → 60`; crawl finished at 60 pages, 4 product pages
  found, 0 unreachable.
- `britanniagifts.us` (large real WooCommerce catalog, ~111 categories, explicitly named by the
  user as a known-good large-catalog target): first attempts surfaced a **real crawl-fairness bug**
  — only 26 of a real ~150-page crawl were product pages, and 10 of those 26 were all from a single
  category (lawn mowers) while ~100 other real categories contributed zero, even though no
  category was anywhere near its own 30-product cap. Root cause: `app/site_mapper.py`'s BFS wave
  loop built `next_wave` by fully appending each source page's children before moving to the next
  page in the batch, and the following iteration's `wave[:room]` truncation always cuts from the
  end — so whichever collection page happened to be processed first in a batch dominated the
  truncated wave entirely, regardless of per-category caps (which only gate *whether* a URL is
  enqueued, not where it lands in `next_wave`). **Fixed** with round-robin interleaving via
  `itertools.zip_longest` across a batch's source pages before enqueueing. Proven with a new
  regression test (`test_tight_overall_budget_still_represents_every_category_fairly`) that
  reproduces the exact shape — confirmed to fail without the fix (one category gets all 10 of a
  tight 10-slot budget) and pass with it (every category gets ≥1, none gets more than
  `ceil(10/3)=4`).
  - A clean live re-confirmation on `britanniagifts.us` itself was not obtainable this session: 3
    repeated crawls of the same site within ~15 minutes escalated its own bot-protection to the
    point of blocking even the homepage — a real, honest signal to back off, not a code problem.
    The fix stands on the isolated live-shaped regression test plus the original live bug report,
    not on a second live "after" crawl of that specific store.

**Part 2 — a real annotated screenshot, live:**

Full audits (LLM grading on, real OpenAI calls) against `vellano.site` and `leafloop.site`
(user-named known-good candidates) surfaced **two real, separate bugs in the screenshot pipeline**
(`app/checks/screenshot_annotator.py`), both root-caused and fixed rather than filed for later:

1. **Clip height was never actually clamped to the viewport.** `clip["width"]` was correctly
   clamped against `viewport["width"] - clip["x"]`, but `clip["height"]` was computed as
   `min(box_height + margin, box_height + margin)` — both sides of that `min()` were literally the
   same expression, so height clamping was a no-op. Whenever the located element was tall enough
   to extend past the 768px viewport, Playwright's `page.screenshot(clip=...)` threw "Clipped area
   is either empty or outside the resulting image" on a real, correctly-located
   `llm_policy_substance_shipping_policy` finding on `leafloop.site`. Fixed by clamping height the
   same way width already was.
2. **`scrollIntoView()` doesn't apply synchronously on a page with `scroll-behavior: smooth`
   CSS** (a common modern theme default — present on `leafloop.site`) — it animates over ~800ms,
   so reading `getBoundingClientRect()` right after (even after a couple of animation frames) can
   return the element's stale, pre-scroll position, hundreds of pixels outside the viewport.
   Confirmed directly against the live page with ad-hoc Playwright scripts: unscrolled position
   `y≈936` vs. correctly-centered `y≈370` after the scroll actually settled. Fixed by explicitly
   passing `behavior: "instant"` to `scrollIntoView()`, which overrides the page's CSS default for
   that one call — confirmed live to produce the correct coordinates with no arbitrary sleep.
3. Also added a bounded `networkidle`-with-fallback settle wait to the screenshot module's second
   page visit (mirroring the pattern `PageFetcher` already uses in `app/fetch.py`, which this
   lightweight second-visit path had never had).

Both fixes are covered by new regression tests
(`test_clip_height_is_clamped_to_the_viewport_like_width_already_is`,
`test_networkidle_timeout_on_second_visit_degrades_to_domcontentloaded_snapshot`).

With all three fixes in place, a real end-to-end run against `leafloop.site` produced **two real,
working annotated screenshots** for two separate real `llm_policy_substance_*` suspension-risk
findings (shipping policy and terms of service), each with the exact evidence quote highlighted in
a red box with sensible margin. Verified rendering in all three export formats: the `.md` report's
image reference, a real embedded JPEG confirmed inside the generated `.docx` (`word/media/*.jpg`),
and two real `/DCTDecode` JPEG streams confirmed inside the generated `.pdf`.

A fourth, separate, real gap was found but deliberately **not** fixed this round: the LLM-graded
`llm_policy_substance_*` check's `evidence` field is not always a strict verbatim quote — on some
runs it was analytical prose describing what the policy page is *missing*, which can never be
located in the DOM by design (there's nothing to highlight). This is a prompt/schema-fidelity
question for the LLM check itself, not a screenshot-pipeline bug, and touching that prompt without
dedicated validation was out of scope for this round — flagged here for future attention rather
than silently accepted as "the feature is fine."

Two other real, incidental findings from this round's live runs, both fixed:

- `PageFetcher`'s hardcoded 6-second wait for a bot-protection JS interstitial to resolve was too
  short for a real store's slower "please wait" challenge (confirmed in a real Chrome tab: it took
  ~7–10s to clear). Promoted to a proper setting, `Settings.crawl_challenge_wait_seconds` (default
  10.0s, clamped to `[1, 30]`), wired through both `app/site_mapper.py` and
  `app/monitor_service.py`'s cheap-check fetcher.
- Repeated live crawling of the same real store within a short window visibly escalates its own
  bot-protection (interstitials → outright blocking the homepage) and, separately, can produce real
  HTTP 503s under load — both correctly degrade to honest `CANNOT VERIFY` findings with specific
  failure categories rather than crashing or misreporting, which is itself a live confirmation that
  the failure-category system built earlier in this project holds up under real, unplanned stress.

**477 tests passing** after this round.

## 18. Explicitly open, not forgotten

- **The accuracy validation set** — scaffolding (`validation/`) exists; still blocked on the user's
  own manual ground-truth pass over 5 real stores.
- **LLM `llm_policy_substance_*` evidence-quote fidelity** (§17) — the model's `evidence` field is
  schema-required to be verbatim but isn't always; a prompt/schema hardening pass would need its
  own dedicated round, not a screenshot-pipeline patch.

Purchase-journey reachability validation, previously open, is now done (§14b) — validated live
against a real checkout via the new disposable Docker+ngrok test store, not deferred any further.
Part 1's live acceptance and the annotated-screenshot live example, both previously open, are now
done (§17) — both surfaced and fixed real bugs live, exactly as this project's own stated
discipline expects.
