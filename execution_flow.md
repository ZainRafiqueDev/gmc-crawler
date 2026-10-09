# Execution Flow

How a request actually travels through this codebase: every real entry point, the order things
call each other in, and (in the Change Log at the bottom) what changed in each editing cycle. Kept
up to date alongside the code — when a cycle changes what calls what, update the relevant section
above the Change Log, then add a Change Log entry describing the shift.

`decisions.md` records *why* a choice was made; this file records *what actually runs, in what
order*. `last.md` is the full feature-by-feature narrative.

---

## The three entry points

Every real way this system starts doing work funnels into the same core pipeline
(`app/graph.py`, next section) — these three just differ in *who* calls it and *what happens to
the result*.

### 1. CLI — `audit.py`

```
python audit.py --url <site> [--wc-key/--wc-secret] [--max-pages] [--max-depth]
                 [--enable-purchase-journey --confirm-test-payment-mode]

main() → asyncio.run(main_async())
main_async():
  load_settings()                                    (app/config.py)
  assert_public_url(url)                              (app/security/ssrf_guard.py — refuse before doing anything else)
  async_playwright().start() → chromium.launch(args=["--disable-dev-shm-usage"])
  run_audit(url, settings, browser, llm_cache, db)     (app/graph.py — the core pipeline, see below)
  [if --enable-purchase-journey] run_purchase_journey_check()   (app/checks/purchase_journey.py)
                                  → regenerates report_markdown to include journey findings
  generate_markdown_report(...) result already computed by the graph; written to disk
  findings_to_csv_bytes(findings) → sibling .csv written next to the .md
  browser.close(); db.dispose()
```

Writes `<host>-<timestamp>.md` and `<host>-<timestamp>.csv` directly to `Settings.report_output_dir`
and exits. No persistence beyond the files on disk.

### 2. API — `app/api/main.py` (FastAPI, run via `uvicorn app.api.main:app`)

```
lifespan() at startup: launches one shared Playwright browser + LLMCache, kept for the app's life

POST /api/audits {url}
  → assert_public_url(url)
  → JobStore.create() (app/api/jobs.py — persists an AuditJobRecord row, status="pending")
  → asyncio.create_task(run_audit_job(...))            ← fires and returns job_id immediately
  → response: {job_id}

run_audit_job()  (app/api/jobs.py, runs in the background)
  → run_audit_streaming(url, settings, browser, llm_cache, on_phase)   (app/graph.py)
      on_phase callback writes job.phase to the DB after every graph node, so...
  GET /api/audits/{job_id} (polled by the frontend) can report real per-phase progress
  → on completion: generate_markdown_report(..., major_only=True) computed too (the major_only
    variant isn't part of the graph's own output - computed here specifically for the job record)
  → JobStore._update(status="done", findings_json=..., report_markdown=..., report_markdown_major_only=...)

GET /api/audits/{job_id}/report.{md,docx,pdf,csv}
  → report.md:  PlainTextResponse(job.report_markdown or major_only variant)
  → report.docx: markdown_to_docx_bytes(markdown, base_dir=report_output_dir)   (app/report_docx.py)
  → report.pdf:  markdown_to_pdf_bytes(markdown, base_dir=report_output_dir)    (app/report_pdf.py)
  → report.csv:  findings_to_csv_bytes(job.findings)                            (app/report_csv.py) — always
                  the full raw list, no major_only variant (by design)

POST /api/monitor/stores {url, mode, ...}  → registers a MonitoredStore row, schedules jobs (see
  entry point 3 below) via MonitorService/SchedulerBackend

POST /api/monitor/stores/{id}/rerun
  → asyncio.create_task(run_store_rerun_job(...))  (app/api/jobs.py)
      → MonitorService.run_full_audit_streaming(store_id, on_phase)   (app/monitor_service.py)
          (same graph call as below, just tracked as an AuditJobRecord instead of only an AuditRun)

GET /api/monitor/stores/{id}/latest-report(.md|.docx|.pdf|.csv)
GET /api/monitor/stores/{id}/runs                 (audit history list, newest first)
GET /api/monitor/stores/{id}/runs/{run_id}        (one historical run's report + its delta)
```

### 3. Scheduler — `app/monitor_service.py` + `app/scheduling.py`

`MonitorService.__init__` registers, per `MonitoredStore` row, whichever of these an
`APSchedulerBackend` job fires on its own interval:

```
run_full_audit(store_id, trigger="manual"|"interval"|"on_change"|"policy_change:<id>")
  → run_audit(store.url, per_store_settings, browser, llm_cache, db)   (app/graph.py — same core pipeline)
  → _persist_run(...): writes an AuditRun row (findings_json, report_markdown,
    report_markdown_major_only, delta_markdown, delta_markdown_major_only)
  → generate_delta_report(platform, site_map, previous_findings, current_findings)  (app/report.py)
    computed against the store's own previous AuditRun, persisted alongside the new one
  → retention pruning (keeps audit_run_retention_count rows, never drops the single most recent)

run_full_audit_streaming(...)   — identical, but drives on_phase progress callbacks (used when a
  rerun is tracked as an AuditJobRecord via the API, see entry point 2)

run_cheap_check(store_id)   — homepage-only fetch + content/DOM hash compare (no full crawl);
  calls run_full_audit(store_id, trigger="on_change") only if the hash actually changed

run_policy_watch()   (app/policy_watcher.py::check_policy_sources under the hood)
  → for every store with on_policy_change=True: run_full_audit(store_id, trigger="policy_change:<id>")
    if a tracked policy area's real source page changed since its last check
```

---

## The core pipeline — `app/graph.py`

All three entry points above eventually call `run_audit()` / `run_audit_streaming()`, which build
and invoke the same compiled LangGraph `StateGraph` (`build_audit_graph()`). Flow control only —
this project does not use LangChain. A shared `AuditState` TypedDict threads through all five
nodes; each node returns a partial dict that gets merged into the state for the next node.

```
detect_platform
  → app/platform_detector.py::detect_platform(url)
      httpx probes for WooCommerce (/wp-json/wc/v3/*), Shopify (/products.json), else generic WordPress/unknown
  state += {platform}

crawl_and_classify
  → app/site_mapper.py::map_site(base_url, browser, settings, platform)
      _adaptive_page_budget()            — sizes settings.crawl_max_pages from a real signal
                                            (WC product-count probe > sitemap catalog count > sitemap
                                            total > configured default), unless explicitly overridden
      _fetch_sitemap_urls() + fetch_wc_product_count()   run concurrently
      _map_site()'s BFS wave loop:
        for each wave: PageFetcher.fetch() each URL concurrently (app/fetch.py)
          → SSRF guard (3 layers) on every request
          → anti-bot interstitial detection + resolution wait
          → app/page_classifier.py::classify_page() on the fetched HTML
        next_wave built via round-robin interleaving across the batch's source pages
          (itertools.zip_longest — fairness fix, see decisions.md)
        _enqueue_if_allowed() enforces per-category product-page caps
      _probe_soft_404_baseline(fetcher, home_norm)   — once per audit, after the crawl loop ends:
          fetches one guaranteed-nonexistent URL (an audit-scoped UUID token) via the SAME fetcher
          → app/change_detection.py::compute_content_hash()/normalize_for_content_hash()
          → stored on the returned SiteMap (soft_404_baseline_content_hash/_normalized_text),
            None/None if the probe itself failed (never blocks/fails the audit)
  state += {site_map}

deterministic_checks
  → app/checks/deterministic.py::run_all_deterministic_checks(site_map)
      check_https, check_required_pages, check_external_links, check_duplicate_nav_footer,
      check_broken_internal_links, check_broken_images (real HEAD/GET probes via httpx)
      check_required_pages() internally calls _split_genuine_from_soft_404() per required page_type,
        which calls app/soft_404_detection.py::is_strong_content_match() (exact-hash or 0.92-
        SequenceMatcher-near-identical, reusing app/checks/duplicate_products.py's threshold) against
        both the audit's soft-404 baseline above AND the homepage's own already-fetched content -
        downgrades a would-be "present" candidate to CANNOT_VERIFY, never independently produces a
        CONFIRMED "missing" finding (see decisions.md)
  → app/checks/business_identity.py::check_business_identity_consistency(site_map)
  → app/checks/form_checks.py::check_forms(site_map)
      _check_action_reachable() — real reachability probe on each form's action URL
  → app/checks/duplicate_products.py::check_duplicate_products(site_map)
  → app/checks/product_checks.py::run_product_checks(site_map, platform, settings)
      WooCommerce (if credentials configured) → app/checks/woocommerce_products.py
      Shopify (no credentials needed)         → app/checks/shopify_products.py
      else / unmatched pages                  → app/checks/generic_product.py
      → app/checks/product_images.py::run_deterministic_image_checks(page_images)
          (missing alt text, placeholder filenames, broken/low-res images - real httpx probes)
  state += {findings, product_images}

llm_grading
  → app/llm/checks.py::run_llm_checks(site_map, settings, cache, db)
      soft_404_flagged_page_urls(site_map)  — computed once at the top (same shared function
        check_required_pages uses, app/soft_404_detection.py) - a required-page candidate flagged
        here is skipped entirely for substance grading below, never independently graded
      per required policy page (skipping soft-404-flagged candidates): check_policy_page_substance()
      homepage + sampled product pages: check_editorial_quality(), check_prohibited_content()
      homepage/product pages matching a shipping/returns claim regex, against whichever
        shipping/returns policy page ISN'T soft-404-flagged: check_claim_policy_contradiction()
      each of the above → app/llm/policy_rag.py::get_policy_context() (real RAG retrieval)
                        → app/llm/factory.py::get_llm_client(settings) → LLMClient.call_tool()
                        → app/llm/checks.py::verify_evidence_quote() on the returned evidence_quote
                          (evidence-fidelity fix - downgrades confidence, never drops the finding)
  → app/llm/image_checks.py::run_llm_image_checks(site_map, product_images, settings, cache)
  → app/impact_tier.py::apply_impact_tiers(all_findings)
  → app/ads_eligibility.py::apply_ads_eligibility_impact(...)
  state += {findings (now impact-tiered), llm_coverage}

compile_report
  → app/checks/screenshot_annotator.py::capture_annotated_screenshots(browser, findings, settings, report_output_dir)
      per distinct page_url with an eligible (LLM-graded, suspension-risk, evidence_verified) finding:
      a second lightweight page visit, live-DOM quote search, cropped/annotated screenshot saved
  → app/report.py::generate_markdown_report(platform, site_map, findings, cache_stats, llm_coverage)
      aggregate_repetitive_findings(findings)   (app/finding_aggregation.py - first line of this
                                                   function; the CALLER's own findings list, used
                                                   for CSV/DB/history, is untouched)
      compute_risk_score(), _build_policy_matrix(), _format_finding_rich()/_format_finding(),
      _page_by_page_block(), _catalog_section_lines(), _final_assessment()
  state += {report_markdown, findings (with screenshot_path set where applicable)}
```

`run_audit()` returns the final `AuditState` dict; `run_audit_streaming()` does the same but
`await`s an `on_phase(node_name)` callback after each node completes (this is the only difference
between the two — same graph, same nodes, same order).

## Export/format layer (called after the graph, never inside it)

```
app/report.py::generate_markdown_report()/generate_delta_report()   → Markdown (source of truth)
app/report_docx.py::markdown_to_docx_bytes(markdown, base_dir)      → .docx (parses the Markdown subset
                                                                        this project actually produces)
app/report_pdf.py::markdown_to_pdf_bytes(markdown, base_dir)        → .pdf  (same parsing approach)
app/report_csv.py::findings_to_csv_bytes(findings)                  → .csv  (structured, from Finding
                                                                        objects directly - not from Markdown -
                                                                        always the raw unaggregated list)
```

---

## Change Log by cycle

Each entry: what was touched, and — critically — whether it changed *what calls what* (not just
internal logic). Newest first. See `decisions.md` for the reasoning behind each; see `last.md` for
the full narrative.

### Cycle: shipping_region_contradiction -> advisory (quality_improvement)

```
load_rules_from_yaml
  -> _validate_advisory(rule)       NEW: quality_improvement rule + policy_reference -> ValueError
  -> null-source warning            now only for NON-quality_improvement rules
build_finding -> _finding_from_rule_evaluation
  rule.impact == QUALITY_IMPROVEMENT -> risk_level="advisory", google_rule=None, advisory consequence
compute_snapshot_status             skips risk_level=="advisory" findings
render_first_audit_report_markdown  risk findings (or "No actionable risks found"), THEN "## Advisories"
```

*What calls what changed:* `app/first_audit_report.py` now imports `ADVISORY_RISK_LEVEL` from
`app.findings_builder`. Nothing else changed in the call graph; the rest is branching inside
existing functions.

### Cycle: login brute-force limit + First Audit concurrency cap / duplicate-run guard

```
POST /api/auth/login    (app/api/auth.py)
  -> _login_limiters(request)               lazily creates app.state.login_ip_limiter /
                                             login_account_limiter (app.security.rate_limiter.RateLimiter)
  -> remaining(ip) == 0 or remaining(email) == 0  -> 429 + Retry-After   (checked BEFORE the password)
  -> verify_password
       fail    -> allow(ip) + allow(email) (records the failure) -> 403
       success -> reset(ip) + reset(email) -> create_session -> Set-Cookie

POST /api/first-audit   (app/api/first_audit.py, require_admin)
  -> assert_public_url
  -> _first_audit_slots(request).reserve(_url_key(url))
       same URL key already in flight -> 409 "already in progress (run_id=N)"
       in-flight count >= first_audit_max_concurrent -> 429
  -> create_first_audit_run -> slots.attach(url_key, run_id)   (release on failure)
  -> asyncio.create_task(_run_and_release(...))   <- NEW wrapper:
       asyncio.wait_for(run_first_audit(...), first_audit_timeout_seconds)
         TimeoutError -> _mark_error(run_id, "timed out after Ns")   (app/first_audit.py)
       finally -> slots.release(url_key)
     task held in slots.tasks so it can't be GC'd mid-run
```

*What calls what changed:* the background task no longer calls `run_first_audit` directly. It goes
through `_run_and_release`. `login` now calls into `RateLimiter`, which is reused unchanged. New
settings: `login_rate_limit_{ip,account}_{max_attempts,window_seconds}`,
`first_audit_max_concurrent` and `first_audit_timeout_seconds`. All of this state is per-process,
so it is correct only with a single uvicorn worker (see Dockerfile).

### Cycle: whole-app admin gating + whole-app redesign (black+light-blue, GSAP, reactbits-style flourishes)

**Backend**: every route in `app/api/main.py` except `/health` now takes `_admin: User =
Depends(require_admin)` - the old `/api/audits` + `/api/monitor/*` pipeline (17 routes) was fully
public before this; now identical to First Audit's own gating. No test fixture changes needed (that
HTTP layer had zero existing test coverage - confirmed by grep before changing anything).

**Frontend auth, restructured from per-page to app-wide:**

```
app/layout.tsx (server component, keeps the `metadata` export)
  -> components/AppShell.tsx ("use client", the one place that decides chrome + gating per route)
       -> lib/auth-context.tsx::AuthProvider  (one GET /api/auth/me on mount, exposes {admin, loading})
       -> components/SiteHeader.tsx           (nav + logout, only rendered when admin is set)
       -> lib/auth-context.tsx::Protected     (blocks children until admin is confirmed; skipped for /login)
```

`lib/first-audit-api.ts` renamed to `lib/admin-api.ts` (it now backs the whole app's session, not just
First Audit). `lib/useAdminGuard.ts` and `app/first-audit/login/` and `app/first-audit/layout.tsx` are
deleted - superseded by the above. `lib/api.ts`'s fetch calls all gained `credentials: "include"` (the
gating change above would otherwise 403 them even when logged in).

**Theme**: `.fa-theme`'s black+light-blue tokens promoted to `app/globals.css`'s `:root` directly (the
First-Audit-only scoping is gone - one theme, one app, one login). New flourish primitives:
`components/ScrollReveal.tsx` (GSAP ScrollTrigger entrance), `components/AnimatedCounter.tsx` (GSAP-
tweened number), `components/SpotlightCard.tsx` (cursor-tracked radial highlight, CSS vars written on
mousemove) and a `.gradient-border` CSS utility. `app/page.tsx` rebuilt around these (hero, live store
count, three feature cards, the existing audit form).

`/monitor`, `/monitor/[storeId]`, `/report/[jobId]`, `/first-audit*` then got a second pass (same
round, after a user checkpoint): every stat tile and history-list item gained the `spotlight-card`
class plus an inline `handleSpotlight` pointer handler (one per file, not the separate `SpotlightCard`
component - these elements already carry their own Framer Motion hover/layout/exit props, so adding a
class+handler to the existing node was simpler than nesting a second card wrapper); every numeric stat
now renders through `AnimatedCounter`; the two long-form report-markdown panels
(`/monitor/[storeId]`, `/report/[jobId]`) are wrapped in `ScrollReveal`. See `decisions.md` for the
light-theme leftovers found and fixed in both passes, including the markdown report view's `prose`
classes, which was the one fix with real content-legibility impact, not just chrome.

### Cycle: First Audit frontend (login, run-trigger, status/poll, PDF download)

New isolated section under the existing Next.js app, `frontend/app/first-audit/`:

```
/first-audit/login          -> POST /api/auth/login (lib/first-audit-api.ts::login)
/first-audit                -> useAdminGuard() checks GET /api/auth/me, redirects to login on 403
                                form submit -> POST /api/first-audit -> router.push(`/first-audit/${run_id}`)
/first-audit/[runId]        -> useAdminGuard() + poll GET /api/first-audit/{id} every 2s (same pattern
                                as app/report/[jobId]/page.tsx) until status is done/error
                                status=done -> <a href={firstAuditReportPdfUrl(runId)}> (plain link,
                                SameSite=Lax cookie still attaches on the top-level GET navigation)
```

`frontend/app/first-audit/layout.tsx` wraps all three pages in a `.fa-theme`-classed panel (new CSS
block in `app/globals.css`, overriding the same custom properties the root indigo/fuchsia theme
defines) and a small local header with a logout button. One `NavLink` added to the root
`app/layout.tsx`; nothing else in the old monitoring UI (`/`, `/monitor`, `/report/[jobId]`) changed.

`lib/api.ts`'s private `asJson` helper is now exported and reused by the new
`lib/first-audit-api.ts` client - every call there passes `credentials: "include"` (the one real
behavioral difference from `lib/api.ts`'s calls, needed because these routes are session-cookie
gated and the API is cross-origin from the frontend dev server).

No backend changes. Live-validated via curl against the real running backend (see `decisions.md`
for the full request sequence) rather than a visual browser click-through - the Chrome extension
wasn't connected in this sandbox.

### Cycle: bot-block detection, blocked-vs-absent distinction, off-domain detect-and-credit, variant_key fix

Four fixes in one round, all test-first, all live-validated against real stores (ridge.com, gymshark.com).

**1. Hard-block detection (`app/fetch.py`)** - a new tier in `PageFetcher.fetch()`, checked
immediately after `page.content()`, *before* the existing transient-challenge wait:

```
fetch(url):
  html = await page.content()
  _looks_like_hard_block(html)?                         NEW - checked first
    -> raise _RetryableFetchFailure(category="bot_blocked")   (same category/cap as before, no new one)
  _looks_like_challenge(html) / _looks_like_captcha(html)?    UNCHANGED, now only reached if not hard-blocked
    -> _wait_for_challenge_to_resolve(...)
  ... success path unchanged
```

`_looks_like_hard_block` matches an unambiguous phrase list unconditionally, and a shorter ambiguous
list only when the page's visible text is also short (interstitial-sized). A hard-blocked page now
surfaces as `result.ok=False`, `likely_bot_blocked=True`, `result.text=None` - never read as reachable
content.

**2. Blocked-vs-absent distinction (`app/rules/engine.py`)** - `_evaluate_missing_resource` and
`_evaluate_all_missing` gained a branch between "genuine/soft-404" and "confirmed fail", mirroring
`app.checks.deterministic.check_required_pages`'s own established priority order:

```
_evaluate_missing_resource(rule, site_map):
  matches = site_map.pages_of_type(page_type)
  genuine, soft_404_flagged = _split_genuine_from_soft_404(reachable, site_map)   UNCHANGED
  genuine?            -> pass
  soft_404_flagged?   -> needs_review (0.5)                                       UNCHANGED
  cannot_verify_matches (blocked/unreachable candidates)?  -> needs_review (0.3)  NEW
  resource_type == returns_policy and find_returns_credit(site_map)?  -> pass     NEW (see cycle 3 below)
  else                -> fail
```

`FirstAuditRun.unreachable_pages_count` (new column) is set in `app.first_audit.run_first_audit` right
after `pages_crawled`, and surfaces as a run-level note in the PDF report
(`app/first_audit_report.py`) and in `FirstAuditStatusResponse` (`app/api/first_audit.py`) - "N pages
could not be read" is now visible to a reader, not silently absorbed into a vague `needs_review`.

**3. Off-domain detect-and-credit (new `app/off_domain_credit.py`)** - called from the same two rule
evaluators above, narrowly: `returns_policy` resource type, and the exact
`{business.email, business.phone}` all-missing field set.

```
find_returns_credit(site_map) / find_contact_credit(site_map):
  for each reachable page in site_map.pages:
    for each link in page.external_links:        <- links already collected during the normal crawl,
                                                      NOTHING new is fetched; SSRF hostname boundary
                                                      is untouched
      same-registrable-domain subdomain (tldextract) + returns/contact keyword in the URL?  -> credit
      OR link's domain in a known returns-portal list (loopreturns.com, narvar.com, ...)?    -> credit
  no match -> None
```

A credit records `matched_via`/`credited_url`/`found_on_page` as evidence - "presence detected, content
not audited," never a silent full pass.

**4. `variant_key` fix (`app/facts/jsonld_facts.py`)** - `_facts_for_offer`'s `variant_key` now derives
from whichever identifier actually distinguishes a variant, not SKU-first:

```
_facts_for_offer(offer, product_sku, product_gtin, product_mpn):
  gtin, mpn_norm, sku_norm resolved as before (offer's own field, falling back to product/group-level)
  variant_key = gtin or mpn_norm or sku_norm or sha256(json.dumps(offer, sort_keys=True))[:16]   CHANGED
                (was: sku_norm or gtin or mpn_norm or f"offer-{offer_index}")
```

The old SKU-first order silently collapsed every size/color variant of a real `ProductGroup` to the
same key, since `sku` is the shared parent value in that shape while `gtin`/`mpn` are what actually
vary. The positional-index fallback is also gone entirely - `_flatten_nodes` splits a `hasVariant`
product into separate top-level node calls (each with its own single-element `offers` list), so a
per-call index always resets to 0 and would silently recreate the same collapse for identifier-less
offers; a content hash of the offer's own fields is stable per-variant instead, and groups genuinely
identical offers together for free.

**Live validation (real, no mocking)**: a deliberately shrunk, targeted live crawl against
gymshark.com (`crawl_max_pages=8`, `crawl_concurrency=1` - a full 150-page/concurrency-4 discovery
crawl proved unreliable in this sandbox, first an OOM kill then a Playwright driver crash; not a
pipeline bug) completed cleanly end to end: `snapshot_status=ACTION_REQUIRED`, not a false CRITICAL.
`missing_returns_page` evaluated to `pass` with `off_domain_credit` evidence pointing at
`support.gymshark.com/en-US/article/returns-policy`. The real product page's JSON-LD produced 7
distinct GTIN-derived `variant_key`s despite every variant sharing the identical SKU `B6C5UUFHB`.

**Test-isolation audit**: grepped every other test file with a bare `Settings()` call against every
LLM-triggering entry point - none are at risk the way `test_first_audit.py` was (already fixed in an
earlier cycle). No changes needed.

Full suite: 706 passed (was 686 before this cycle's first fix, 689 after the blocked-vs-absent fix).

### Cycle: LLM-assisted extraction for the four "free-prose" commerce facts

Found live during the three-store validation round: regex keyword-proximity extraction for
`return.window_days` produced two confirmed false positives on modcloth.com (a refund-processing
sentence mistaken for the return window; the real window sentence missed because it never says
"return"). `app.facts.extractor.extract_all_facts` is now async and takes `settings`/`cache`:

```
extract_all_facts(site_map, settings, cache):
  business_identity_facts + commerce_facts (shipping.region/cost, payment.methods,
                                             return.cost_responsibility - UNCHANGED, deterministic)
  + product_jsonld_facts (UNCHANGED, deterministic)
  + await extract_llm_assisted_commerce_facts(site_map, settings, cache)   (app/facts/llm_commerce_facts.py, NEW)

extract_llm_assisted_commerce_facts(site_map, settings, cache):
  eligible_pages = pages whose type can state ANY of the 4 facts (union of the existing page-type sets)
  not settings.llm_configured?  -> _regex_only_commerce_facts(pages)   (exact pre-existing behavior, unchanged)
  else:
    client = get_llm_client(settings, cache)
    for each eligible page (bounded concurrency, asyncio.gather):
        _call_llm_for_page(client, page)     -> one "submit_commerce_facts" tool call, all 4 facts at once
    for each (page, llm_result):
        _reconcile_page(page, llm_result):
            per fact: regex_fact = <the SAME existing regex function, reused unchanged>
                      _reconcile_one(fact_key, page, regex_fact, llm_value, llm_sentence, llm_confidence):
                          llm_value is None -> no fact (LLM's "nothing here" wins over any regex hit)
                          verify_evidence_quote(llm_sentence, page_text) fails -> no fact
                          regex_fact agrees with llm_value -> method="regex_llm_agree", confidence=0.95 (fixed)
                          else -> method="llm", confidence=<the model's own reported value, clamped [0,1]>
```

`app.first_audit.run_first_audit` now constructs an `LLMCache(db)` and awaits
`extract_all_facts(site_map, settings, llm_cache)` - the only call site, updated.
`app.facts.commerce_facts.extract_commerce_facts` no longer emits the 4 moved facts directly; its
regex functions stay in that module, imported by `llm_commerce_facts.py` as the fast-path input -
not duplicated.

Live-validated (real OpenAI call, not a fake): both real modcloth.com pages now return
`return.window_days=30` with confidence 1.0 - the refund-processing trap and the no-literal-"return"
miss are both gone, end to end.

Also found and fixed during this cycle's own test run (not before): three `test_first_audit.py`
integration tests started making real LLM calls because a real `ANTHROPIC_API_KEY` happened to be in
this machine's `.env` and bare `Settings()` picked it up - fixed by explicitly blanking both API keys
in that test file's `_settings()` helper.

### Cycle: consequence/risk_level fix + admin auth + First Audit API + PDF report download

**Fix**: `app.findings_builder`'s `risk_level`/`consequence` now derive from the *finding's own*
severity (`_RISK_LEVEL_BY_SEVERITY`/`_CONSEQUENCE_BY_SEVERITY`), never from the parent rule's
`impact` tier - an `existing_check`-delegated finding can have a milder severity than the rule's own
worst case, and inheriting the rule's impact produced a real "medium severity, suspension risk"
inconsistency in the live run's own output.

**New entry points**, mounted in `app/api/main.py`:

```
POST /api/auth/login    (app/api/auth.py)  -> verify_password -> create_session -> Set-Cookie
POST /api/auth/logout                       -> delete_session -> clear cookie
GET  /api/auth/me                           -> current user, or 403 if not logged in

POST /api/first-audit                       (app/api/first_audit.py, require_admin)
  -> create_first_audit_run + asyncio.create_task(run_first_audit(...))   (unchanged from piece 4)
GET  /api/first-audit/{run_id}              (require_admin) -> status/snapshot_status
GET  /api/first-audit/{run_id}/report.pdf   (require_admin)
  404 if run missing; 409 if run.status != "done"
  else: SELECT FindingRecord WHERE audit_run_id=run_id
        -> render_first_audit_report_markdown(run, findings)    (app/first_audit_report.py -
                                                                   findings ordered critical-first;
                                                                   zero findings -> "No actionable
                                                                   risks found", still a real PDF)
        -> markdown_to_pdf_bytes(markdown, ...)                 (app/report_pdf.py - REUSED verbatim,
                                                                   only its _BRAND color changed)
        -> Response(media_type="application/pdf")
```

`require_admin` (`app/auth/dependencies.py`) reads the session cookie, looks up `Session`+`User` via
`app.auth.sessions.get_user_for_token`, 403s if missing/expired/non-admin. `seed_admin` runs once in
`app/api/main.py`'s `lifespan`, right after `db.init()`.

Not built this round: a frontend login page, or any first-audit findings-list/detail UI (so "the
findings UI links to this PDF" could not be confirmed - that UI doesn't exist).

### Cycle: First Audit pipeline, step 4 piece 4 (rule rename, rule engine, findings, PASS-drops-body, screenshots) - FINAL PIECE

`app.first_audit.run_first_audit` (renamed from `run_first_audit_discovery` - it now runs the whole
pipeline, not just discovery) is the single entry point end to end:

```
run_first_audit(run_id, url, settings, browser, db):
  status -> "running"; assert_public_url(url)
  detect_platform(url) -> map_site(...)                    (REUSED, unchanged)
  persist_resources + extract_all_facts + persist_facts     (prior cycles, unchanged)

  rules = load_rules_from_yaml()                            (app/rules/loader.py - now validates every
                                                              rule's fact key/resource_type/method
                                                              against app/facts/keys.py's registry)
  sync_rules_to_db(rules)

  evaluations = run_rule_engine(session, run_id, rules, site_map)   (app/rules/engine.py)
    per rule, dispatch by condition.type:
      cross_page_contradiction / numeric_mismatch -> app.contradiction_engine (piece 3, unchanged)
      missing_resource   -> reuses app.checks.deterministic._split_genuine_from_soft_404 + crawl_totally_failed
      all_missing        -> bounded ResourceFact existence query, same crawl_totally_failed guard
      existing_check      -> dynamically imports + calls the named function (e.g.
                              check_business_identity_consistency), wraps each returned Finding as an
                              Evaluation (evidence_json["delegated_finding"] = the Finding, verbatim)

  findings = build_findings_for_run(session, rules_by_id, evaluations)   (app/findings_builder.py)
    pass -> no Finding. fail/needs_review -> FindingRecord with google_rule/consequence/remediation/
    store_evidence (both sides, rendered per condition type) all populated - never partial.
  run.snapshot_status = compute_snapshot_status(findings)   (highest-severity-wins: CRITICAL > AT_RISK > ACTION_REQUIRED > COMPLIANT)

  _apply_body_evidence_retention(...)                        (app/first_audit.py, piece 4 step 4)
    failing_resource_ids = every evaluation's own resource_id, PLUS every resource cited as evidence
                            inside a site-wide evaluation (observations list, or - found live - a
                            delegated existing_check finding's own evidence TEXT, URL-regex-scanned)
    per resource -> store_resource_body_evidence(...)         (app/evidence_store.py, step 3 - now finally called)

  _capture_finding_screenshots(...)                           (app/first_audit.py, piece 4 step 5)
    only evaluation.result == "fail" (never needs_review) AND severity critical/high (when
    settings.screenshots_only_critical, the default) -> capture_finding_screenshot_bytes(browser, ...)
                                                              (app/checks/screenshot_annotator.py - new
                                                               blob-returning sibling of the old
                                                               file-writing capture_annotated_screenshots;
                                                               both share one WebP-encoding implementation)
    -> write_blob_if_needed(..., "image/webp") -> finding.screenshot_blob_hash

  run.status -> "done"
```

Two real bugs were found and fixed during this cycle's own live end-to-end run (not by unit tests,
which used synthetic evidence that didn't expose either): (1) a delegated existing_check finding's
cited pages weren't being recognized for body retention at all (no `observations` shape, no single
`page_url`) - fixed by also regex-scanning the delegated finding's evidence text for URLs; (2)
`_apply_body_evidence_retention`/`_capture_finding_screenshots` were mutating ORM objects loaded in an
earlier, already-closed session - silently never persisted. Fixed by re-fetching by id
(`session.get(...)`) inside the session doing the actual write.

Live validation: ran the full pipeline against britanniagifts.us (real Playwright browser, zero
mocking, capped to 30 pages) - 30 pages crawled, WooCommerce detected, 35 facts extracted, 2 real
findings (missing returns page; two different phone numbers across 4 real pages), snapshot_status
CRITICAL, 4 resources' bodies correctly retained as evidence, 26 correctly dropped.

### Cycle: First Audit pipeline, step 4 piece 3 (contradiction engine)

New module, not yet called from `app.first_audit` (piece 4 wires it in, once rules drive which
fact keys/sources/methods to compare - this piece only builds and tests the comparison machinery
itself):

```
evaluate_cross_page_contradiction(session, audit_run_id, rule_id, fact_key, source_types, min_confidence)
                                                        (app/contradiction_engine.py)
  SELECT ResourceFact JOIN Resource WHERE audit_run_id=... AND fact=fact_key
                                     [AND Resource.resource_type IN source_types]
  < 2 rows?  → return None (absent elsewhere is not a conflict)
  group rows into equivalence classes via values_equal(fact_key, a, b)   (app/facts/normalize.py)
  1 class  → Evaluation(result="pass", resource_id=None)
  2+ classes → Evaluation(result="fail" if min(confidences) >= min_confidence else "needs_review")
             evidence_json carries BOTH sides: value/source_url/method/confidence/resource_type per observation

evaluate_per_resource_method_comparison(session, audit_run_id, rule_id, fact_key, methods, tolerance, min_confidence)
  SELECT ResourceFact JOIN Resource WHERE audit_run_id=... AND fact=fact_key AND method IN methods
  group by (resource_id, variant_key)             ← SKU-keyed, not page-keyed; never crosses resources
  for each group missing either method            → skip (not a conflict)
  for each group with both methods present         → numeric tolerance compare
                                                      → one Evaluation(resource_id=resource_id, ...)
```

Both functions only ever write `Evaluation` rows - no `Finding`/`FindingRecord`, no
`store_resource_body_evidence` call (piece 4's job, once it knows which resources actually failed).

### Cycle: First Audit pipeline, step 4 piece 2 (JSON-LD -> same canonical product facts)

`app.facts.extractor.extract_all_facts` now also walks every `PRODUCT`-type page in the site map:

```
extract_all_facts(site_map):
  facts = extract_business_identity_facts(site_map) + extract_commerce_facts(site_map)   (piece 1, unchanged)
  for page in site_map.pages:
      facts += extract_product_jsonld_facts(page)        (app/facts/jsonld_facts.py, piece 2)
        _parse_jsonld_blocks(page.html)                    → every <script type="application/ld+json">,
                                                              malformed ones skipped per-block, never fatal
        _flatten_nodes(block)                               → recursively unwraps @graph/arrays into plain nodes
        [node for node if @type == "Product"]
        _facts_from_product_node(url, node):
          brand/itemCondition → product.brand / product.condition   (normalize_condition)
          _normalize_offers(node["offers"])                 → single Offer | array (variants, kept separate)
                                                                | AggregateOffer (lowPrice + any real nested offers)
          per offer → product.price.amount/.currency, product.availability (normalize_availability),
                      product.sku/.gtin/.mpn (normalize_identifier / normalize_gtin)
```

Every fact here is tagged `method="jsonld"` - the same canonical keys piece 1's (future) platform-
API/visible-HTML product facts will use, which is what lets the rule engine (piece 4) compare a
JSON-LD price against a platform-API price for the *same* `product.price.amount` key instead of two
unrelated ones. `jsonld_offer`/`jsonld_product` (two method tags from the original canonical-facts
draft) were collapsed into this single `jsonld` tag before any code used them - both had the
identical 0.95 base confidence, so nothing was lost.

Not yet built: the contradiction engine (piece 3), rule-engine evaluation (piece 4).

### Cycle: First Audit pipeline, step 4 piece 1 (fact extractors: business identity + shipping/returns/payment)

Canonical fact dictionary locked first (`app/facts/canonical_facts.md`), then extractors built
against it - no extractor code existed before the dictionary did. `run_first_audit_discovery`
(previous cycle) now does one more thing after `persist_resources`:

```
run_first_audit_discovery(...):
  ...
  resources = persist_resources(session, run_id, site_map)
  resources_by_url = {r.url: r for r in resources}
  facts = extract_all_facts(site_map)                    (app/facts/extractor.py)
    = extract_business_identity_facts(site_map)           (app/facts/business_identity_facts.py)
      + extract_commerce_facts(site_map)                  (app/facts/commerce_facts.py)
  persist_facts(session, resources_by_url, facts)          (app/evidence_store.py) → one ResourceFact
                                                             row per FactRecord, linked by resource.id
```

`extract_business_identity_facts` imports `app.checks.business_identity`'s own `_extract_signals`,
`_guess_calling_code`, `_CALLING_CODE_COUNTRIES`, `_IDENTITY_PAGE_TYPES` directly (reused, not
duplicated) for `business.email`/`.phone`/`.address`/`.address_country`; business name/legal entity
are new regex patterns (copyright-footer, "operated by ...") since that module never extracted them.

`extract_commerce_facts` is all-new pattern matching (no existing check to generalize) for
`shipping.region`, `shipping.cost.amount`/`.currency`, `shipping.processing_days`/`.delivery_days`,
`return.window_days`/`.refund_terms`/`.cost_responsibility`, `payment.methods` - every fact routed
through shared `app/facts/normalize.py` helpers (country-name table, money symbol map, day-range
parser, availability dual-vocabulary map, GTIN-14/identifier canonicalization) so two extractors
never normalize the same kind of value two different ways.

Every `FactRecord` (`app/facts/types.py`) gets its `confidence` from `app.facts.keys.METHOD_BASE_CONFIDENCE`
(per extraction method, not hand-tuned per instance) - the rule engine (piece 4) will compare this
against each rule's own `min_confidence` to decide `fail` vs `needs_review`.

Not yet built: product/JSON-LD facts (piece 2), the contradiction engine (piece 3), rule-engine
evaluation (piece 4).

### Cycle: First Audit pipeline, step 3 (discovery + classification + evidence store) + evidence-storage schema

New entry point, parallel to the existing CLI/API/monitoring ones above, not yet wired into any of
them (no new route added this round - `app/first_audit.py` is called directly in tests only, until
work-order step 5 adds the admin-gated API route):

```
create_first_audit_run(db, url)                       (app/first_audit.py) → FirstAuditRun row, status="pending"
run_first_audit_discovery(run_id, url, settings, browser, db):
  status → "running"
  assert_public_url(url)                                (app/security/ssrf_guard.py - reused, same guard as the CLI/API paths)
  detect_platform(url)                                  (app/platform_detector.py - REUSED AS-IS)
  map_site(base_url, browser, settings, platform)        (app/site_mapper.py - REUSED AS-IS)
  persist_resources(session, run_id, site_map)           (app/evidence_store.py) → one Resource row per CrawledPage
  status → "done", platform/pages_crawled recorded
```

`persist_resources` is intentionally thin: it writes `url, resource_type, http_status, content_hash,
discovered_via` only - no raw body, no facts, no rule results. Fact extraction, JSON-LD, the
contradiction engine, and the rule engine (work-order step 4) are NOT called from this path yet.

**Evidence storage** (Tier 2, added to the schema in the same cycle per a mid-round spec addition):

```
store_resource_body_evidence(session, adapter, settings, resource, page, has_failing_evaluation, jsonld_raw=None)
                                                        (app/evidence_store.py - called by work-order step 4's rule
                                                         engine once evaluations exist, NOT by persist_resources)
  if not (has_failing_evaluation or settings.keep_body_on_pass):
      release any existing body/jsonld blob on this resource, clear the hash columns, return
  else:
      write_blob_if_needed(session, adapter, settings, page.html, "text/html")   (app/evidence_blob_store.py)
        → content_hash_for_blob(data)                    (sha256 of UNCOMPRESSED bytes - the dedup key)
        → session.get(Blob, hash): exists? bump refcount/last_referenced_at, done - no upload
                                    missing? compress(data, codec) → adapter.put(hash, ...) → insert Blob row
      same for jsonld_raw if given → resource.jsonld_blob_hash
```

`adapter` is `app.evidence_storage.get_storage_adapter(settings)` → `LocalFileStorageAdapter`
(default, one file per hash under `EVIDENCE_BUCKET`) or `S3StorageAdapter` (lazy `import boto3`,
only constructed if `EVIDENCE_BACKEND=s3` is actually selected).

**Retention** - scheduled daily in `app/api/main.py`'s `lifespan`, same `APSchedulerBackend` the
policy-watch job already uses:

```
run_retention_job(db, adapter, settings)                (app/evidence_retention.py)
  → prune_evidence_blobs(db, adapter, settings): for each FirstAuditRun outside the configured
      retention window (days and/or run-count), release_blob() each of its resources'/findings'
      *_blob_hash references, clear the columns - resource_facts/evaluations/findings untouched
  → prune_llm_cache(db, settings): delete LLMCacheEntry rows beyond llm_cache_max_items, oldest first
```

### Cycle: counterfeit banned-word list (client requirement, premium/luxury/designer sellers)
- **`app/llm/checks.py`**: `_COUNTERFEIT_GUIDANCE` (the prompt text `check_prohibited_content`
  passes to the model) extended with a client-provided banned-word list (fake, imitation, cloned,
  dupe, copy - replica/knockoff/inspired by/mirror quality already covered) plus explicit
  negation-safety wording for the newly-added, common-in-benign-contexts words. No new function, no
  new call site, no new check_id - same `llm_prohibited_content` check, same call
  (`run_llm_checks` → `check_prohibited_content` → `client.call_tool(...)`), richer system prompt
  text only.
- Live-validated with two real LLM calls each way (risky vs. benign use of the same new words) -
  see decisions.md for the actual evidence quotes returned.

### Cycle: soft-404/LLM-substance redundant-finding fix
- **New shared function**: `app/soft_404_detection.py::soft_404_flagged_page_urls(site_map)` -
  extracted from (and now the single implementation behind) `app/checks/deterministic.py`'s
  `_split_genuine_from_soft_404`, which now just calls it instead of recomputing its own inline
  comparison. **New caller**: `app/llm/checks.py::run_llm_checks` now calls it too (once, near the
  top) and checks membership before queuing `check_policy_page_substance` (both the real-LLM branch
  and the `llm_configured=False` placeholder-finding branch) and before selecting a shipping/returns
  policy page in `_claim_contradiction_tasks`. No new node, no new call into a different module from
  `app/graph.py` - this is entirely inside the existing `deterministic_checks`/`llm_grading` nodes'
  own internals.
- Confirmed live (`leafloop.site`, a real GoDaddy-parked-domain site): before this fix, the report
  showed both the soft-404 CANNOT_VERIFY finding *and* a redundant "lacks required substance"
  finding for the same `/lander` page; after, only the former.

### Cycle: soft-404/catch-all detection
- **`app/site_mapper.py`**: new `_probe_soft_404_baseline(fetcher, home_norm)`, called once at the
  end of `_map_site()` (after the crawl loop, before returning the `SiteMap`) - reuses the SAME
  `fetcher`/`PageFetcher` instance the whole crawl already used. Calls
  `app/change_detection.py::compute_content_hash()`/`normalize_for_content_hash()` (the latter newly
  extracted from the former's own internals, no behavior change to existing callers).
- **`app/models.py`**: `SiteMap` gained two new fields (`soft_404_baseline_content_hash`,
  `soft_404_baseline_normalized_text`) populated by the call above.
- **New module `app/soft_404_detection.py`** (`is_strong_content_match`) - imports
  `app.checks.duplicate_products._NEAR_DUPLICATE_THRESHOLD` directly (reuse, not a new constant).
  Called only from one place: `app/checks/deterministic.py`'s new `_split_genuine_from_soft_404()`
  helper.
- **`app/checks/deterministic.py`**: `check_required_pages()`'s existing `if reachable: continue`
  branch is now preceded by the genuine/soft-404 split above; a new branch emits a CANNOT_VERIFY
  finding when a required page_type's only reachable candidate(s) are all soft-404-flagged. Every
  other branch in this function (cannot-verify, language-unsupported, confirmed-missing) is
  unchanged and still reached exactly as before when no reachable candidates exist at all.
- **`app/fetch.py`**: new `"likely_soft_404"` entry in the three shared `FAILURE_CATEGORY_*` dicts -
  referenced directly by `check_required_pages`'s new branch, never written onto
  `CrawledPage.failure_category` itself (that field's contract stays "why a fetch failed";
  soft-404 is a content-level judgment on a *successful* fetch - see decisions.md).
- **Confirmed, not changed**: `app/graph.py`'s node order (`deterministic_checks` before
  `llm_grading`) and `app/llm/checks.py::run_llm_checks`'s `if page.reachable` gate already meant
  the LLM layer never independently decides page existence - verified by reading the code and by a
  new regression test, no call-flow change needed there.

### Cycle: report bloat aggregation + CSV export
- **New call**: `app/report.py::generate_markdown_report()` and `generate_delta_report()` now call
  `app/finding_aggregation.py::aggregate_repetitive_findings()` as their first line — every
  downstream section in both functions now operates on the aggregated list, not the raw one passed
  in. The caller's own `findings` variable is untouched (aggregation happens on a local rebind).
- **New module**: `app/finding_aggregation.py` — no other module calls into it besides `report.py`.
- **New module**: `app/report_csv.py` (`findings_to_csv_bytes`) — called from three places: `audit.py`'s
  `main_async` (writes a sibling `.csv`), `app/api/main.py`'s two new routes
  (`/api/audits/{id}/report.csv`, `/api/monitor/stores/{id}/latest-report.csv`).
- **Frontend**: both report-viewing pages (`frontend/app/report/[jobId]/page.tsx`,
  `frontend/app/monitor/[storeId]/page.tsx`) gained a "Download full detail (.csv)" button calling
  the new routes via `frontend/lib/api.ts`'s extended `reportDownloadUrl`/`latestReportDownloadUrl`.
- No change to the graph itself or to any check function's own logic.

### Cycle: evidence-quote fidelity verification
- **New calls inside `app/llm/checks.py`**: each of the four LLM-graded check functions
  (`check_policy_page_substance`, `check_editorial_quality`, `check_prohibited_content`,
  `check_claim_policy_contradiction`) now calls the new `verify_evidence_quote()` (same module)
  immediately after the LLM tool-call result comes back, before constructing the returned `Finding`.
- **New field, no new call**: `Finding.evidence_verified` (`app/models.py`) - read by `app/report.py`'s
  three finding-rendering functions to decide whether to append the "could not be independently
  verified" note.
- No change to `run_llm_checks`'s own call order, and no live-browser call added anywhere in this
  cycle (the whole point of this design - see decisions.md).

### Cycle: crawl-fairness + screenshot pipeline fixes
- **`app/site_mapper.py`**: `_map_site`'s BFS wave loop internals changed (round-robin interleaving
  via `itertools.zip_longest` instead of sequential per-page appending) - no new function calls, the
  same functions (`_enqueue_if_allowed`, `classify_page`) are called, just in a different order/pattern.
- **`app/checks/screenshot_annotator.py`**: `_capture_one`'s clip-height computation fixed (no call
  change); `_FIND_QUOTE_JS`'s in-page `scrollIntoView()` call gained an explicit `behavior: "instant"`
  argument (no Python-level call change, only the injected JS itself); a new
  `page.wait_for_load_state("networkidle", ...)` call added to the second-visit navigation, mirroring
  `app/fetch.py::PageFetcher`'s existing pattern.
- **`app/config.py` / `app/site_mapper.py` / `app/monitor_service.py`**: new
  `Settings.crawl_challenge_wait_seconds`, threaded into both `PageFetcher(...)` construction call
  sites (previously a hardcoded default with no call-site parameter at all).

*(Earlier cycles - adaptive page-budget scaling, per-category caps, audit history, policy-change
re-audits, the screenshot mechanism's initial build, purchase-journey validation, the production
deployment fixes - predate this file; see `last.md` for their full detail. Add new cycles above
this line as they happen, newest first.)*
