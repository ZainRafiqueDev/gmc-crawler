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
