# GMC Compliance Checker

Audits a live WordPress/WooCommerce store against Google Merchant Center
(GMC) policies and produces a Markdown findings report. Crawls the real
rendered site (Playwright, not `requests`), so JS-rendered/SPA pages don't
get silently skipped.

Platform detection is advisory only - every check runs regardless of
platform, using an API-verified path when a platform's product data is
reachable and falling back to a page-only best-effort check otherwise.
Every LLM-graded check cites real, live-scraped Google Merchant Center
policy text via a real RAG index (Phase C - see below), not a hand-written
stub summary.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate        # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements-dev.txt
playwright install chromium
cp .env.example .env          # set an API key (see below) to enable LLM-graded checks

python audit.py --url https://example.com
```

Optional flags:

```bash
python audit.py --url https://example.com \
  --wc-key ck_xxx --wc-secret cs_xxx \
  --max-pages 100 --max-depth 3 \
  --output-dir ./reports \
  --no-cache   # disable the LLM/vision result cache for this run
```

Without an LLM API key configured, the deterministic checks (steps 1-4)
still run in full - the LLM-graded checks (step 5: policy page substance,
editorial quality, prohibited-content screening) are reported as `CANNOT
VERIFY` instead of being silently skipped.

## LLM provider

Set `LLM_PROVIDER=claude` (default) or `LLM_PROVIDER=openai` in `.env`,
plus the matching API key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`). Both
providers implement the same `LLMClient` interface (`app/llm/client.py`)
via forced structured output - Claude's forced tool-use, OpenAI's forced
function-calling in strict Structured Outputs mode - so switching is a
config change, not a code change. Every prompt requires a verbatim evidence
quote and an explicit `location` field (which element/section of the page)
as part of the structured output, for both providers.

Vision checks (image mismatch + prohibited-content screening) work the
same way on both providers - image passed by URL, no local download - with
the same hard rule: the prohibited-content flag is always `potential_risk`
confidence, never `confirmed`, regardless of what the model itself reports.

Live-validated with `LLM_PROVIDER=openai` (`gpt-4o-mini`) against real
stores: a real policy-substance finding through the full pipeline, and a
direct vision call against a real product image that correctly matched the
product description with real (non-fabricated) reasoning, plus a
deliberately-wrong-price test proving the mismatch-detection branch fires
on real data.

## Architecture

The pipeline is orchestrated with LangGraph
(`app/graph.py`): `detect_platform -> crawl_and_classify ->
deterministic_checks -> llm_grading -> compile_report`.

- `app/platform_detector.py` - WordPress/WooCommerce detection via the
  `/wp-json/` REST API index, Shopify detection via the public
  `/products.json` endpoint, with direct-namespace and HTML-marker fallbacks
  for sites that disable both. The result is advisory only - nothing
  downstream refuses to run because the platform is unrecognized.
- `app/fetch.py` - the resilient fetch layer every page goes through:
  headless Chromium render, network-idle wait (falls back to
  `domcontentloaded` if the page never quiesces), retry with exponential
  backoff (3 attempts default). A confirmed 404/410 is *not* retried and is
  never reported as `CANNOT VERIFY` - only genuine reliability failures
  (timeouts, 5xx, nav errors) get that label, and only after every retry is
  exhausted.
- `app/site_mapper.py` - BFS crawl seeded from the homepage + `sitemap.xml`
  (including sitemap indexes), depth/page-count capped via `Settings`.
- `app/page_classifier.py` - classifies each crawled page into a `PageType`
  using URL patterns plus title/heading/main-content text signals. Nav,
  header, footer, and cookie-consent boilerplate are stripped before the
  text signal is used - a "see our Privacy Policy" cookie banner appears on
  almost every page and will misclassify everything if left in.
- `app/checks/deterministic.py` - HTTPS enforcement, required policy pages,
  every external-domain link (with exact link + source page), broken
  internal links/images, and duplicate nav/footer template blocks (3+
  near-identical copies only - 2 copies is normal responsive
  desktop/mobile-menu behavior, not a bug).
- `app/checks/business_identity.py` - extracts email/phone/address wherever
  they appear (footer, contact, policy pages) and flags inconsistencies,
  including phone-country-code vs. stated-address mismatches.
- `app/checks/product_checks.py` - dispatcher that picks the best available
  product-page verification path and always produces a full result, never a
  hard failure: WooCommerce with credentials -> `woocommerce_products.py`
  cross-check against `/wp-json/wc/v3/products`; Shopify -> `shopify_products.py`
  cross-check against the public `/products.json` (no credentials needed,
  but it can be blocked/password-protected); anything else, or any product
  page the API path couldn't match -> `generic_product.py`'s page-only
  check (visible price + availability signal). Every `Finding` carries a
  `verification_method` (`api_verified` vs `page_only`) so the report can
  honestly label how much to trust a pass/cannot-verify.
- `app/llm/` - Claude or OpenAI checks (provider-agnostic, see below), forced
  into structured tool-use output so results are parseable, not regexed out
  of prose. Every finding cites the real, live-scraped GMC policy text
  retrieved for that specific check (Phase C's RAG index - see below), not
  a hand-written stub. Prompts explicitly require verbatim evidence quotes
  to prevent hallucinated evidence.
- `app/checks/product_images.py` - product image checks: broken/404 images,
  resolution below `MIN_DIMENSION_PX`, missing alt text, placeholder/stock
  filename heuristics (`placeholder.png`, `default-product.jpg`, etc.).
  Images are sourced from the same WooCommerce/Shopify API response
  `product_checks.py` already fetched (no duplicate calls) when available -
  `api_verified` - or scraped from the page's `<img>` tags otherwise -
  `page_only`.
- `app/llm/image_checks.py` - Claude vision check (image passed by URL,
  Claude fetches it server-side - no local download/base64 needed): flags a
  product photo that doesn't plausibly match its title/description, and
  separately flags anything that might raise a GMC prohibited-content
  concern. The prohibited-content flag is *always* `potential_risk`
  confidence, never `confirmed`, regardless of what the model itself
  reports - this is a human-review screening signal, not a policy
  determination, by design.
- `app/checks/purchase_journey.py` - opt-in, off-by-default purchase-journey
  check: adds a sample product to cart, loads the cart page and verifies
  the price, loads the checkout page and checks for a shipping charge and a
  consistent total, then stops. It is structurally incapable of going
  further - there is no code path in this module that clicks a place-order/
  pay control or submits a payment form; the flow is a fixed, small
  sequence of actions that ends after reading the checkout page. Gated in
  `audit.py` behind **both** `--enable-purchase-journey` and
  `--confirm-test-payment-mode` - omitting either refuses to run. **Not
  live-validated** - it needs a real store you own/control with a sandbox
  payment gateway enabled (WooCommerce Test Mode, Shopify's Bogus Gateway);
  proven instead with a mocked Playwright browser asserting the click count
  never exceeds 1 (the add-to-cart button) across every tested scenario.
- `app/report.py` - compiles findings into the Markdown report shape:
  executive summary, critical/high/medium/low sections, page-by-page
  pass/risk/cannot-verify findings, and a prioritized fix list. A page's
  pass/risk status is derived from whether any finding references it, not
  tracked separately.
- `audit.py` - CLI entry point for a one-shot audit.

## Configurable monitoring (`monitor.py`)

Register a store once and let it re-check itself on a schedule, instead of
re-running `audit.py` by hand:

```bash
# re-run a full audit every N days
python monitor.py register --url https://example.com --mode interval --interval-days 3

# cheap homepage hash-check every day; only runs the full audit when it changes
python monitor.py register --url https://example.com --mode on_change --cheap-check-interval-days 1

# both at once
python monitor.py register --url https://example.com --mode both --interval-days 7 --cheap-check-interval-days 1

python monitor.py list
python monitor.py run-full --store-id 1          # trigger a full audit now
python monitor.py run-cheap-check --store-id 1   # trigger a cheap check now
python monitor.py policy-check                    # trigger the policy-source watcher now
python monitor.py serve                           # run the scheduler daemon (foreground, blocking)
```

- `app/db.py` - SQLAlchemy async models (`MonitoredStore`, `PageSnapshot`,
  `AuditRun`, `PolicySourceSnapshot`). `DATABASE_URL` defaults to a local
  `sqlite+aiosqlite` file (zero setup) and accepts `postgresql+asyncpg://...`
  (Supabase is fine, per the original brief) with no code changes - same
  models either way.
- `app/change_detection.py` - content-hash + DOM-structure-hash per page.
  The cheap on_change check re-fetches only the homepage (one page, not a
  full crawl) and compares its hash against the last stored snapshot; a
  full audit snapshots every crawled page, for future page-level diffing.
- `app/scheduling.py` - `SchedulerBackend` protocol + an `APSchedulerBackend`
  implementation. `MonitorService` only calls through the protocol, so
  swapping to Celery/RQ later is one new class, not a rewrite.
- `app/monitor_service.py` - registration, the cheap change check (triggers
  a full audit only when a real diff is found), the full audit (snapshots
  every page, stores findings, and generates a delta report against the
  previous full-audit run if one exists), and wiring stores' jobs into the
  scheduler.
- `app/policy_watcher.py` - independent of any store's schedule (its own
  interval, default weekly): re-fetches a small set of confirmed real GMC
  Help Center pages (`POLICY_SOURCE_URLS`), hashes the content, and flags
  which ones changed since last checked - findings citing a changed policy
  may now be grading against a stale requirement.
- `app/report.py` also exposes `generate_delta_report()` - new/resolved/
  changed/unchanged issues since the last run, reusing the same finding
  formatting as the full report.

Live-validated: registered a local test server for `on_change` monitoring,
ran a cheap check (first run = baseline, triggers a full audit), edited the
served page, ran the cheap check again (real content+DOM hash change
detected, triggered a second full audit + a delta report showing the actual
new/changed findings), then ran it once more unchanged (correctly a no-op).
Also caught and fixed a real bug this way: a store URL with a `:port` (e.g.
`http://localhost:8917`) produced a 0-byte report file on Windows, because
a bare `:` in a filename is parsed as an NTFS Alternate Data Stream
separator - `app/report.py:safe_host_for_filename()` sanitizes it now.

## Purchase-journey verification (Phase E, opt-in)

```bash
python audit.py --url https://your-test-store.example \
  --enable-purchase-journey --confirm-test-payment-mode
  # optional: --purchase-journey-product-url https://your-test-store.example/product/widget
```

Off by default; both flags are required together. Only run this against a
store you control with a sandbox/test payment gateway enabled - see
`app/checks/purchase_journey.py`'s module docstring for the exact safety
design before changing anything in that file.

## Testing

```bash
pytest -v
```

219 tests, zero live network/browser calls - HTTP is mocked with `respx`,
Playwright's `Browser`/`Page` are mocked directly, both LLM provider
clients are faked with canned tool-call responses, and the monitoring
tests use a temp-file SQLite DB with a fake scheduler backend. The SSRF
guard's own tests (`tests/test_ssrf_guard.py`) deliberately exercise the
real DNS-resolution logic against literal IPs (127.0.0.1, the cloud
metadata endpoint, etc.) rather than mocking it - a global `conftest.py`
fixture neutralizes that same real-DNS behavior for every *other* test
using fake domains, so the suite stays fast and network-free everywhere
else. The external-domain-link and business-identity-consistency checks
are proven against a constructed fixture site (`tests/conftest.py`), per
the Phase 1 acceptance criteria.

## Cost/speed hardening

- **LLM/vision result cache** (`app/llm/cache.py`) - `CachedLLMClient` wraps
  the real provider client transparently (check functions don't change at
  all), keyed by `sha256(provider|model|tool_name|content)` - content is
  the exact page text for grading checks, or just the image URL for vision.
  Same content -> same key -> cache hit -> no new API call, which is also
  what makes a scheduled re-audit of an unchanged store cheap (wired into
  `monitor_service.py`'s `run_full_audit`) with no separate "did this page
  change" bookkeeping needed. Every cached `Finding` is marked
  `from_cache=true`. Max age 30 days as a safety net; `invalidate_all()` is
  ready for Phase C's policy re-embed job to call once that exists. Live-
  validated: two back-to-back real audits of the same store went from 0
  hits/1 miss (real OpenAI call, ~6s) to 1 hit/0 misses (no new call), total
  wall time 77s -> 57s.
- **Tiered LLM checking** - grading only ever runs on page types where it
  adds real judgment value (policy pages, homepage, product pages);
  collection/cart/checkout/contact/FAQ/blog pages are never sent to the LLM
  - deliberate, documented in `run_llm_checks`'s docstring, not incidental.
- **De-boilerplated prompts** - the same nav/header/footer/cookie-consent
  stripping the page classifier already does (`CrawledPage.main_content_text`)
  is reused for LLM-graded prompts too, cutting tokens and removing noise
  that could distract grading.
- **Image dedup** - `run_llm_image_checks` groups by image URL before
  calling vision, so the same image reused across product variants/pages is
  graded once and the result applied to every page it appears on (URL-based;
  true content-hash dedup across different URLs serving identical bytes
  isn't implemented).
- **Crawl-priority seeding** - `site_mapper.py` now splits sitemap.xml URLs
  into "priority" (product/policy-looking, via
  `page_classifier.looks_like_priority_url`) and everything else, seeding
  priority URLs into the very first crawl wave ahead of homepage-discovered
  nav links. Fixes the crawl-prioritization gap below - reserves up to a
  third of the page budget (min 10) for these before generic collection/
  category pages can consume it.
- Concurrent page fetches were already implemented (`asyncio.Semaphore`
  bounding a per-wave `asyncio.gather`, hard-capped at `HARD_MAX_CONCURRENCY`)
  from Phase 1 - not new this round, just confirmed and documented as
  deliberate rather than incidental.
- **Not implemented**: skipping unchanged pages' *rendering* entirely
  during a full audit (only their LLM grading is skipped, via the cache).
  Doing this reliably would mean trusting Last-Modified/ETag headers on
  JS-rendered storefronts, which is often unreliable - the honest call was
  to not build a half-working version of this rather than claim it works.

## Security hardening

- **SSRF protection** (`app/security/ssrf_guard.py`) - blocks
  private/loopback/link-local/reserved/multicast IP ranges (including the
  169.254.169.254 cloud metadata endpoint) and non-http(s) schemes, applied
  at three layers on every fetch, unconditionally - no toggle, no fallback
  path that skips any of them:
  1. A fail-fast check on the user-supplied URL before any work starts
     (`audit.py`, `monitor_service.register_store`).
  2. Per-request re-validation before dispatch - a custom httpx transport
     for every httpx call (`safe_async_client`), and a Playwright
     `context.route()` guard for every browser request (`install_ssrf_guard`,
     installed on every context, every attempt). The Playwright guard uses
     `route.fetch()` + `route.fulfill()` rather than `route.continue_()` -
     the latter, even completely unmodified, was observed live to make a
     real WooCommerce store's chunked response arrive truncated to a
     body-less `<head>` under Chromium/CDP; explicitly fetching and handing
     back the exact response avoided it (verified across repeated live runs
     plus a dedicated test where a page's own `fetch()` to the cloud
     metadata address was aborted with zero bytes sent).
  3. A post-navigation check of the actual final URL. This is *not*
     redundant with (2): verified live that Chromium does not expose
     intermediate redirect-chain hops as separately interceptable requests,
     for either `route.continue_()` or `route.fetch()`+`fulfill()` - a
     fulfilled 3xx response is followed by the browser as an internal
     navigation that never raises a second `context.route()` event. httpx's
     transport genuinely does re-invoke itself per redirect hop, so layer 2
     alone covers httpx; Playwright fetches need layer 3 as the backstop for
     "the redirect landed somewhere bad," since layer 2 there only ever sees
     a request's *own* URL, not where a chain of redirects ends up.

  Every successful fetch reports positive, countable confirmation - not
  just an absence of violations - via `ssrf_requests_validated`/
  `ssrf_requests_blocked` (`CrawledPage`, surfaced in the report's executive
  summary as "all three layers active on every one of N/N page fetch
  attempt(s)"). Live-validated with a real (unmocked) Playwright browser:
  `127.0.0.1`, `10.0.0.5`, and the metadata endpoint were all refused with
  zero navigation attempted; a real 40-page crawl of a real WooCommerce
  store showed all three layers active on 40/40 pages (minimum 45 requests
  validated per page, 0 blocked) while still loading every page correctly.
- **Rate limiting** (`app/security/rate_limiter.py`) - in-memory sliding-
  window limiter, tested under a simulated 20-request burst. Wired into
  `POST /api/audits` (`app/api/main.py`), keyed by client IP; defaults to 5
  audit requests/hour, configurable via `AUDIT_RATE_LIMIT_MAX_REQUESTS` /
  `AUDIT_RATE_LIMIT_WINDOW_SECONDS`.
- **Resource bounds** (`app/config.py`) - hard ceilings (`HARD_MAX_PAGES`,
  `HARD_MAX_DEPTH`, `HARD_MAX_CONCURRENCY`, `HARD_AUDIT_TIMEOUT_SECONDS`,
  `HARD_MAX_IMAGE_BYTES`) enforced via pydantic validators with
  `validate_assignment=True`, so a value can't sneak past the cap either at
  construction or via a later direct attribute set (e.g. a CLI flag
  override). Image downloads are streamed with a hard byte cap rather than
  buffered fully into memory first.
- **Report sanitization** (`app/security/sanitize.py`) - every finding
  field that can contain content lifted from the audited (possibly
  malicious/compromised) site - title, evidence, location, policy
  reference, recommended fix, page title/error - is HTML-escaped and
  control-character-stripped before it goes into the generated report, so
  a `<script>` payload sitting on a target page's title can never execute
  when the report is later rendered as HTML.
- **robots.txt + consistent User-Agent** (`app/security/robots.py`) -
  fetched once per crawl, consulted before every URL is enqueued (fails
  open if missing/unreachable, per standard convention); every outbound
  request across the project uses one identifiable User-Agent
  (`GMC_AUDIT_USER_AGENT`).
- **Secrets never logged** - audited every log/print statement; only
  non-secret settings (page limits, output dir, timeouts) are ever logged.
  `.env` is gitignored; `.env.example` stays blank.

## Frontend

A minimal Next.js app (`frontend/`) against the FastAPI backend
(`app/api/main.py`):

- **Home** (`/`) - paste a store URL, run an audit.
- **Report** (`/report/[jobId]`) - polls job status every 2s, shows live
  phase-by-phase progress (`detect_platform` -> ... -> `compile_report`),
  then renders the finished report's Markdown in-browser, with Markdown/
  docx/PDF download buttons and a form to register the store for
  monitoring. A report generated by an on-demand store re-run (below) shows
  a "Delta report" badge instead, and skips the register-for-monitoring
  form since the store is already registered.
- **Monitored stores** (`/monitor`) - lists registered stores (mode,
  interval, last full audit) with a link to each one's latest report, a
  "Re-run now" action (see below), and lets you remove one.
- **Store report** (`/monitor/[storeId]`) - the latest completed report for
  one monitored store, with the same three download formats and its own
  "Re-run audit now" action.

**On-demand re-run**: "Re-run now" triggers a fresh full audit through the
exact same job-creation flow as a brand-new ad-hoc audit (same rate limiter,
same phase-progress polling, same download endpoints) - but the work itself
goes through `MonitorService.run_full_audit_streaming`, the same
store-monitoring pipeline a scheduled re-audit uses, so the result lands in
that store's real `AuditRun` history and gets diffed into a delta report
against its previous run via the existing `generate_delta_report` (not a
separate/duplicated implementation).

No auth, no styling polish - matches what was asked for. A "history view"
(browsing more than the single most recent report per monitored store) is
still not built into the UI, but the underlying data isn't ephemeral
anymore: ad-hoc "Run Audit" jobs and monitored-store reports both persist
to the database (see below), so it's a UI gap, not a data-loss one.

The in-browser report view caps how much Markdown it renders at once
(`INLINE_RENDER_LIMIT` in `components/ReportView.tsx`) since a messy real
site can produce a report with 1,000+ findings that would otherwise stall
the tab; the download buttons always have the complete report.

Run it:

```bash
# Terminal 1 - backend
uvicorn app.api.main:app --port 8010

# Terminal 2 - frontend
cd frontend
npm install
npm run dev
```

Don't add `--reload` on Windows: uvicorn's reload supervisor changes the
asyncio event loop policy in a way that breaks Playwright's subprocess
launch (`NotImplementedError` from `asyncio.create_subprocess_exec` at
startup) - confirmed live. Restart the process manually after code changes
instead.

`frontend/.env.local` points it at the backend
(`NEXT_PUBLIC_API_BASE_URL=http://localhost:8010`); the backend's
`API_CORS_ORIGIN` (default `http://localhost:3000`) must match wherever the
frontend is actually served from.

### Job/report persistence and retention

Both the frontend's ad-hoc "Run Audit" jobs (`AuditJobRecord`) and
monitored-store audit runs (`AuditRun`) are stored in the database, not
process memory - a backend restart doesn't orphan a job's status, findings,
or downloadable report. Verified live: started a job, let it finish, killed
the server process (not a graceful shutdown), restarted it, and confirmed
`GET /api/audits/{id}` plus both `report.md`/`report.docx` downloads still
worked against the same job ID with no re-run - and separately, that a job
still `running` at the moment of a kill comes back as `status: "error"`
with an "Interrupted by a server restart" message on the next startup,
rather than showing `running` forever with no process actually working on
it (`JobStore.mark_interrupted_jobs_as_errored`, called once in `lifespan`).

Retention (neither is unbounded): monitored-store `AuditRun` rows keep only
the most recent `AUDIT_RUN_RETENTION_COUNT` (default 10) full audits per
store - delta reports only ever need the single latest one, so pruning
older rows is safe. Ad-hoc `AuditJobRecord` rows aren't tied to a store, so
they're pruned by age instead: anything older than
`AUDIT_JOB_RETENTION_DAYS` (default 30) is deleted the next time a job is
created. Both are plain Settings fields, override via `.env` like anything
else.

## Known limitation

The site mapper's BFS crawl could previously exhaust its entire
`--max-pages` budget on one shallow tier (dozens of top-level collection
pages) before ever reaching individual products or policy pages - observed
live on a real Shopify store with ~40 top-level collections. Fixed this
round via crawl-priority seeding (see above); a generously large page
budget still helps on stores with very deep catalogs.

## Phase C: real RAG policy index

Every LLM-graded check retrieves its grounding text from real, live-scraped
GMC Help Center pages instead of the hand-written stub summaries in
`app/llm/policy_snippets.py` (which remain only as the fallback when
retrieval has nothing to return - logged as a warning, since that
shouldn't normally happen once the index exists).

- **Sources** (`app/policy_watcher.py::POLICY_SOURCE_URLS`) - 8 policy
  areas, each with one or more confirmed real Help Center URLs.
  shipping_policy/returns_refunds/business_identity/misrepresentation/
  prohibited_content/editorial_quality each have a dedicated, single-topic
  page. privacy_policy and terms_of_service do not - GMC's real guidance
  for these is spread across a few broader pages (checkout requirements,
  the account-suspension troubleshooting guide, the onboarding checklist)
  rather than one dedicated article each; documented in-code rather than
  forcing a misleadingly clean single-URL citation.
- **Chunking** (`app/llm/policy_chunker.py`) - splits at real heading
  boundaries (h1/h2/h3), not arbitrary character counts, so each chunk is a
  coherent, citable unit; long sections are further split on sentence
  boundaries only if needed.
- **Embedding** (`app/llm/embeddings.py`) - OpenAI `text-embedding-3-small`,
  used regardless of `LLM_PROVIDER` (Claude has no embeddings API - this
  needs `OPENAI_API_KEY` set even when grading uses Claude). Reuses the
  existing `LLMCache` so re-embedding unchanged text is a cache hit.
- **Storage** (`app/db.py::PolicyChunk`) - chunk text + embedding as a
  JSON-encoded float list, not a native pgvector column. The corpus is a
  few hundred chunks at most (184 from the real initial build), so a full
  Python-side cosine-similarity scan is effectively instant; a real ANN
  vector index buys nothing at this scale and would add a hard
  Postgres+pgvector-extension dependency this project doesn't otherwise
  need. Swapping to `pgvector.sqlalchemy.Vector` later is a contained
  change to this one module if the corpus ever grows enough to matter.
- **Retrieval** (`app/llm/policy_rag.py::get_policy_context`) - genuinely
  dynamic per check: the query embedded is the actual page text being
  graded, so two different pages checked against the same policy area can
  surface different top-N chunks. Every finding's `policy_reference` cites
  the real source URL + section retrieved, e.g. `GMC policy: Editorial and
  professional content quality [editorial_quality] -
  https://support.google.com/merchants/answer/12079604 ("What you can
  do")`.
- **Freshness** (`app/policy_watcher.py::check_policy_sources`) - on a
  first check or a detected change to any of a policy area's source pages
  (combined-hash comparison across all of them), that area's chunks are
  fully re-scraped/re-chunked/re-embedded, and the entire `LLMCache` is
  invalidated (`LLMCache.invalidate_all`) - a finding graded against stale
  policy text shouldn't be silently trusted after the real policy changes.

Live-validated: built the real 184-chunk index (8 policy areas, actual
OpenAI cost **$0.0004** - 18,446 tokens at $0.02/1M); ran a real audit and
confirmed findings cite real source URLs + sections, spot-checked 5 of them
directly against the live Google pages (all accurate, including one
weaker-but-real match for terms_of_service, honestly reflecting that area's
thinner underlying source material rather than a retrieval bug); simulated
a policy change (seeded a fake prior hash against the real page) and
confirmed the affected policy area's 41 chunks were genuinely re-embedded
(new timestamps) and a seeded unrelated cache entry was correctly wiped.

One real bug found and fixed during this: `--no-cache` (a CLI debug flag)
used to silently disable RAG retrieval too, as a side effect of deriving
the RAG index's DB access from the LLM result cache's own DB handle -
fixed by threading `db` through the pipeline as its own parameter,
independent of whether LLM result caching happens to be enabled (the
frontend/API path was never affected - it always constructs both
together).

## Not yet built / deliberately deferred

- **Purchase-journey live validation (deferred, pending a test store)** -
  code is built and unit-tested (see above) but not proven against a real
  store; needs one you own/control with sandbox payment enabled.
- **Alerting webhook (deferred, pending your notification pattern)** - you
  have existing WhatsApp/Slack plumbing from other projects; nothing built
  here yet so as not to invent a competing integration.
- Image-based watermark/stock-photo-detection beyond the filename heuristic
  in `product_images.py`
- True content-hash (not just URL) image dedup for vision checks
- **Per-finding "re-verify this"** - re-running just the check(s) behind one
  specific finding (e.g. to quickly confirm a fix) instead of a full
  re-crawl. Would need a targeted single-check execution path distinct from
  the crawl-then-check-everything pipeline everything else here reuses -
  real new plumbing, not a small addition, so deliberately not built this
  round; "Re-run audit now" (full re-audit + delta report) is built instead.
- Frontend has no auth - anyone who can reach the Next.js app can trigger
  audits and manage monitored stores; fine for local/internal use, not for
  putting on the open internet as-is
- Frontend history view - only the single most recent report per monitored
  store is retrievable/downloadable; older `AuditRun` rows exist in the DB
  (kept per the retention policy below) but nothing in the UI lists them

## Legacy code

`legacy/` holds a previous, differently-scoped iteration of this project (a
product-feed / GMC-API auto-connect bot, not a site crawler) that predates
this rebuild. It's kept for reference only and is not part of the current
pipeline or test suite.
