# GMC Compliance Checker — Application Flow, Start to Now

This is the **flow** document: what actually happens, step by step, when the system runs — from the moment a URL is submitted (or a scheduled trigger fires) to the moment a report exists — plus the development flow that got the system to its current state. `final.md` is the round-by-round chronicle (what broke, what was found live, in order); `final2.md` is the topical reference (every feature, organized by what it does). This traces the *path a request actually takes* through the code.

---

## 1. The three entry points, one shared pipeline

Every audit — however it starts — ends up calling the exact same `app/graph.py::run_audit` / `run_audit_streaming`. Nothing downstream of that point knows or cares which entry point it came from.

```
audit.py (CLI)  ─┐
app/api/main.py ─┼──►  app.graph.run_audit(url, settings, browser, llm_cache, db)
monitor.py       ─┘         (or run_audit_streaming, which additionally
 (scheduler,                 reports phase-by-phase progress via on_phase())
 interval/on_change)
```

- **CLI** (`audit.py`): parses flags, validates the URL against the SSRF guard *before* touching the pipeline at all (fail fast), opens one shared Playwright browser for the whole run, calls `run_audit`, writes the Markdown report to disk. The one CLI-only extra step: if `--enable-purchase-journey` is passed (and explicitly confirmed with `--confirm-test-payment-mode`), the purchase-journey check runs *after* `run_audit` completes and the report is regenerated to fold its findings in — this check was deliberately kept out of the main graph, since it has real side effects (an actual cart mutation) the rest of the pipeline never has.
- **API** (`app/api/main.py`): a job-based wrapper — `POST /api/audits` creates an `AuditJobRecord` in the DB, kicks off `run_audit_streaming` as a background task, and the frontend polls job status. Every phase transition (`on_phase` callback) is persisted immediately, so a backend restart mid-audit doesn't orphan the job — it comes back as "interrupted," not stuck "running" forever (verified live: killed the process mid-audit, confirmed on restart).
- **Monitor scheduler** (`monitor.py`, `app/monitor_service.py`): APScheduler-backed. A store registered `interval` gets `run_audit_streaming` called on a timer; a store registered `on_change` gets a cheap hash-diff check first (`app/change_detection.py`) and only calls `run_audit_streaming` if a policy page's content actually changed. Every completed run is diffed against the store's previous run (`generate_delta_report`) and both are persisted to `AuditRun`.

At the point `run_audit` is called, one more thing happens before the graph itself starts: **the opt-in proxy contextvar is set for the whole run** (`set_current_proxy`, from `app.proxy_config`), scoped so it's torn down in a `finally` regardless of how the run ends — this has to happen above the graph, not inside `crawl_and_classify`, because `detect_platform` (the very first node) also makes outbound httpx calls that should respect the same proxy setting.

---

## 2. Inside the graph: one URL becomes a report

`app/graph.py` — a LangGraph `StateGraph` used purely for flow control (this project does not use LangChain; see §4 for what actually keeps the LLM checks grounded). Five nodes, each reading and extending one shared `AuditState` dict:

```
url: str
  │
  ▼
┌─────────────────────┐
│ detect_platform      │──► PlatformDetectionResult (platform, base_url, evidence)
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ crawl_and_classify    │──► SiteMap (pages: list[CrawledPage])
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ deterministic_checks  │──► findings: list[Finding]  (+ product_images dict, for reuse below)
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ llm_grading            │──► findings extended with LLM/vision results, then
└─────────────────────┘      apply_impact_tiers → apply_ads_eligibility_impact
  │
  ▼
┌─────────────────────┐
│ compile_report         │──► report_markdown: str
└─────────────────────┘
```

### 2.1 `detect_platform`
One `httpx` client, several sequential probes, first confident signal wins: `/wp-json/` namespace index → direct `/wp-json/wc/v3/products` / `/wp-json/wp/v2/` probes → Shopify's public `/products.json` → HTML marker fallback (meta generator tag, `wp-content`/`woocommerce` strings, `cdn.shopify.com` references) → `unknown`. This result is **advisory only** — nothing later in the flow branches on it except which product-data API to *try first* (§2.4) and whether to show the "connect your WooCommerce API" recommendation in the final report.

### 2.2 `crawl_and_classify` — the flow inside the flow
This is the biggest sub-flow, `app/site_mapper.py::map_site`:

1. Build (or no-op) a proxy rotator for this specific crawl's Playwright contexts, and re-set the httpx proxy contextvar (so `map_site` is correct even called standalone, outside `run_audit`'s own scope).
2. Load `robots.txt`. If the homepage itself is disallowed: stop here, return an empty `SiteMap` flagged `robots_disallowed=True` — nothing downstream tries to crawl anything, and the report will say so plainly (§2.5) rather than guessing.
3. Fetch `/sitemap.xml`, fall back to `/wp-sitemap.xml`. Split any URLs found into three priority tiers (Store Overview pages first, Catalog/product pages second, everything else last).
4. **Wave-based BFS**: fetch the homepage (wave 0), then priority-seeded sitemap URLs (wave 1, capped at ~1/3 of the page budget), then whatever those pages link to, wave by wave, until the page/depth budget is exhausted. Each page fetch goes through `PageFetcher.fetch` (§2.2.1) inside a bounded semaphore (`crawl_concurrency`).
5. Each fetched page is classified (`app/page_classifier.py`) — URL patterns first (checked against 6 languages), then heading text, then body text, `BLOG_OTHER` if nothing matches (and *only* body text if there was no distinct heading to begin with — see §5 for why).
6. Return the assembled `SiteMap`.

#### 2.2.1 One page's fetch flow (`app/fetch.py::PageFetcher.fetch`)
This is where most of the crawl-robustness engineering lives:

```
for attempt in 1..max_attempts:
    assert_public_url(url)               # SSRF layer 1, retried on transient DNS failure
    wait for this domain's politeness slot  # per-domain min-delay throttle
    open a fresh browser context + install per-request SSRF guard (layer 2)
    page.goto(url)
      │
      ├─ 404/410 → return confirmed_not_found=True immediately, never retried
      ├─ networkidle wait (falls back to domcontentloaded snapshot if it never quiesces)
      ├─ dismiss a cookie-consent banner if one is recognized (best-effort)
      ├─ capture html; does it look like a JS challenge/CAPTCHA interstitial?
      │     yes → poll up to `challenge_wait_seconds`, re-check
      │           still blocked → tag bot_blocked / captcha_blocked, stop retrying this category early
      │           resolved     → proceed with the resolved content, ignore the now-stale status code
      ├─ 429 → back off using real Retry-After if present, retry
      ├─ 401/403 → retry, but capped lower than the general budget (max_bot_block_attempts)
      ├─ other ≥400 → retry as http_error
      ├─ body-less document (a known CDP-interception artifact) → retry
      └─ assert_public_url(page.url)      # SSRF layer 3 — the actual final URL, post-redirect
      → success: return html/text, with real accumulated SSRF-guard stats
```

Every non-success exit carries one specific `failure_category` (never a generic "failed") — this string is what `CrawledPage.failure_category` and, eventually, every downstream "why couldn't this be checked" sentence in the report is built from.

### 2.3 `deterministic_checks`
Six checks run over the finished `SiteMap`, no LLM involved: `check_https`, `check_required_pages`, `check_external_links`, `check_duplicate_nav_footer`, `check_broken_internal_links`, `check_broken_images` (all in `app/checks/deterministic.py`), then `check_business_identity_consistency`, `check_forms`, `check_duplicate_products`, and `run_product_checks` (price/stock, API-verified where credentials/availability allow).

**One gate sits ahead of the two riskiest checks**: if `site_map.crawl_totally_failed` (nothing was ever reachable, homepage included), `check_required_pages` and `check_business_identity_consistency` both short-circuit to a single honest `CANNOT_VERIFY` finding each, stating the real failure category, instead of confidently reporting up to 6 "Missing"/"No contact info" findings from zero actual information.

### 2.4 `llm_grading`
Only runs if `settings.llm_configured`; otherwise every required policy page gets an honest `CANNOT_VERIFY` placeholder instead of silently skipping. When configured:

1. Get a provider client (`app/llm/factory.py` → Claude or OpenAI, based on `Settings.llm_provider`).
2. Queue tasks, bounded by `_LLM_CONCURRENCY` (3):
   - one `check_policy_page_substance` per reachable required-policy page,
   - one `check_editorial_quality` for the homepage and up to 5 product pages,
   - one `check_prohibited_content` for those same product pages,
   - a pre-filter-gated `check_claim_policy_contradiction` for homepage/product pages that actually mention a shipping/returns-adjacent claim, each checked against the matching (shipping-vs-shipping, returns-vs-returns) policy page.
3. Every one of those calls first does a **real RAG retrieval** (`app/llm/policy_rag.py::get_policy_context`) — embed the page's own text, cosine-similarity-rank the indexed chunks for that policy area, return the real retrieved text + source URL(s) + the earliest `created_at` among them (citation freshness). Only then is the actual grading prompt built, with that retrieved text embedded as the requirement the page is being checked against.
4. Each call is a **forced tool-use / structured-output** call — the model cannot return free text, it must fill a schema that includes a verbatim `evidence_quote`. A response is only turned into a `Finding` if the schema's own verdict field says there's a problem; a clean page produces no `Finding` at all (same "absence of findings = pass" convention as the deterministic checks).
5. Separately, `run_llm_image_checks` (`app/llm/image_checks.py`) grades product images already gathered during the deterministic product-check pass, reusing the image list rather than re-fetching.
6. All results are merged with the deterministic findings, then two post-hoc, non-mutating passes run over the *whole* combined list: `apply_impact_tiers` (suspension/disapproval/quality, grounded in real GMC policy text per check_id) and `apply_ads_eligibility_impact` (ads-and-listings/listings-only/unclear, grounded the same way — see `app/ads_eligibility.py`).

### 2.5 `compile_report`
`app/report.py::generate_markdown_report`. The one branch that overrides everything else: if `site_map.crawl_totally_failed`, the very first thing rendered is a `## This Audit Could Not Run` banner stating the specific reason, and `compute_risk_score`'s `crawl_totally_failed` flag flows through as `RiskScore.not_applicable` — every later section (Executive Summary, At a Glance, Policy Matrix, Final Assessment) reads *that one field* rather than each re-deriving the same fact, which is exactly the design fixed into place after a real bug where three sections disagreed with each other about whether the crawl had succeeded.

Otherwise: split findings into suspension-risk vs. everything else (`is_suspension_risk_finding` — a Critical finding, or any confirmed finding whose grounded tier is `suspension_risk`), compute the risk score with its full itemized/reproducible breakdown, render the Suspension Risk section in the rich per-finding format (Location, Relevant Policy, Specific Policy Requirement, Why It Matters, Ads Eligibility Impact, Recommended Fix, Official Source-with-freshness-date), render the confidence-aware Policy-by-Policy matrix, render Other Findings (same rich fields, just deprioritized), render Page-by-Page (Store Overview per-page, Catalog grouped by canonical URL), render Required Fixes (same predicate as the suspension section, not a separate one), render the Final Assessment.

`report_docx.py`/`report_pdf.py` re-parse this same Markdown rather than rendering from `Finding` objects directly — one source of truth, three output formats.

---

## 3. What happens after the graph returns

- **CLI**: report written to `report_output_dir/{host}-{timestamp}.md`.
- **API job path**: `report_markdown` (and, precomputed at the same time, `report_markdown_major_only`) persisted on the `AuditJobRecord`/`AuditRun` row — the major-only toggle in the frontend never re-crawls or re-grades, it's just picking which pre-rendered string to serve.
- **Monitoring path**: the new `AuditRun` is diffed against the store's previous one (`generate_delta_report`, matching findings by `(check_id, page_url)` identity) and both the full and delta reports are persisted. Retention (`audit_run_retention_count`, default 10 per store) prunes older rows — the single most recent run is never pruned, since delta reports always need a previous run to compare against.

---

## 4. The grounding mechanism, end to end (why an LLM finding can be trusted)

Not a framework feature — three deliberate layers, all hand-built:

1. **Forced schema** — the model physically cannot skip the `evidence_quote` field or return prose instead of a structured verdict.
2. **Real retrieval, not a canned prompt** — `policy_rag.py`'s cosine-similarity search over actually-scraped, actually-embedded GMC Help Center chunks, re-run per page (a different page's text can surface different chunks for the same policy area).
3. **A live freshness watcher** (`policy_watcher.py`) hash-diffs the real source pages on its own schedule; a real change re-indexes and invalidates every cached grading result — a finding is never silently graded against text Google has since changed.

On top of those three: the `Finding.policy_requirement_text` the RAG layer computed at grading time is preserved (not discarded once the citation string is built) and rendered for *every* finding regardless of tier — a bug where the quality-tier renderer silently dropped this field was found and fixed live.

---

## 5. Development flow — from scratch to now

This is the compressed version; `final.md` has the full narrative per round, including exactly what was found live and how each fix was verified.

1. **Original build** — the core pipeline, platform detection, crawling, page classification, deterministic checks, the LLM layer (provider-agnostic, forced tool-use), monitoring subsystem, security hardening (SSRF guard's three layers, rate limiting, hard resource ceilings, report sanitization), frontend, and the Phase C real RAG index replacing hand-written policy stubs. Validated live against real stores (`britanniagifts.us`, `iana.org`) — found and fixed a bot-detection/viewport issue, an SSRF-guard CDP-corruption bug, and a `/my-account` misclassification bug along the way.
2. **Broader crawl-robustness round** — HTTP-status-aware retry/backoff (429/403/503/network-level all distinct), anti-bot JS-interstitial and cookie-consent handling, i18n classification coverage for 5 languages plus an honest downgrade-not-guess safety net, and the honest-failure-reporting discipline (never confirm "missing" from an incomplete crawl, and now say *why* it was incomplete) that every later round kept building on. Validated against 13 real, diverse, user-supplied stores.
3. **Score-contradiction / SSRF-honesty / RAG-parity round** — closed the "different sections disagree about crawl success" bug class at the root (a single `RiskScore.not_applicable` flag, not per-section re-derivation), fixed two compounding SSRF-guard-stat bugs found live (a genuine 20-second timeout was showing "0 requests validated"), and fixed the RAG-grounding-parity gap (real policy text was being dropped for every finding outside the suspension-risk section).
4. **WooCommerce review-form / report-polish round** — found and fixed a real false-positive pattern (WordPress's own comment form misdetected as a broken contact form, 10 false suspension-risk findings on one real store), a score-floor transparency gap, a 503-categorization gap, and generalized the page-classification "don't guess from a stray reference" fix from an earlier round.
5. **Ads-eligibility / confidence-aware-matrix / claim-contradiction round** — real, live-researched ads-vs-listings grounding; a confidence-aware Policy Matrix status; a new claim-vs-policy contradiction LLM check that was live-tested into real false positives, hardened with a deterministic backstop (not prompt wording alone), and re-verified clean; citation freshness dates; a counterfeit/brand-risk prompt refinement folded into the existing prohibited-content check.

**406 automated tests today** (up from an original baseline in the low 300s), full suite green after every round above — every fix in this list has a regression test, and every "found live" claim in this document was verified against a real, currently-reachable store during that round, not assumed from the code alone.

---

## 6. What's next — confirmed scope, not yet built

Two features were specified with explicit "stop and confirm before building" gates. Both gates are now resolved (this session); implementation hasn't started yet.

### 6.1 Annotated screenshots for Suspension Risk Findings
- **Scope, confirmed**: Suspension Risk Findings only, for v1 — consistent with every other richer-detail feature (RAG citations, Why It Matters, ads-eligibility tagging) being built there first.
- Within that: deterministic findings already carry a real CSS selector (direct fit); LLM findings need a locatable anchor (see below); **site-wide aggregate findings (e.g. a phone-number inconsistency spanning multiple pages) are skipped entirely for screenshots — confirmed decision, not a partial/misleading exhibit**.
- **The anchor design for LLM findings**: never ask the model to invent a selector directly (reopens exactly the hallucination risk this project has spent multiple rounds closing). Instead, DOM-text-search the already-rendered page for the finding's own already-verified verbatim `evidence_quote` to find its containing element — reusing an existing, already-trusted signal rather than adding a new, less-trustworthy one. If the quote can't be located at screenshot time (content may have changed since grading), skip that finding's screenshot rather than guessing at the wrong element.
- Remaining open engineering calls (not user-blocking, to be made and documented during implementation): cropped-region-with-margin vs. full-page-with-highlight; capture inline during the original crawl/grading pass vs. a lightweight second visit for in-scope findings only.
- Storage: image files under a per-audit directory alongside `report_output_dir`, referenced by relative path in Markdown, embedded natively in docx/PDF (the same precedent as the existing real-table rendering for the Policy Matrix), compressed/resized before embedding.
- **Acceptance for that round**: at least 2 real annotated screenshots from a real store, correctly highlighted, with the existing policy-source citation still rendering correctly alongside each.

### 6.2 Audit history browsing UI + policy-change-triggered re-audits
- **Part 1 (build first, per the spec's own ordering)**: `GET /api/monitor/stores/{id}/runs` listing every retained `AuditRun` (reusing the existing report-rendering path, not a second one), a history view on the frontend's Store Report screen, each entry opening its own real report and showing the delta vs. the previous run (`generate_delta_report` already computes this per run — just needs surfacing per historical pair, not only latest-vs-previous). Must reflect the real retention window (10 runs/store), not imply more history exists than is kept.
- **Part 2**: a new `on_policy_change` store-registration mode, combinable with the existing `interval`/`on_change` modes (same pattern, not a replacement). When `policy_watcher.check_policy_sources` detects a real change to one of the 8 tracked policy areas:
  - **Confirmed**: trigger a full re-audit of *every* store registered for `on_policy_change`, not only stores with past findings in that specific policy area — a store with no past findings there isn't necessarily unaffected by a newly *added* requirement, and cost at current monitored-store scale should stay low (this is the lever to revisit if monitored-store count grows enough to matter).
  - Wired through the existing `run_full_audit`/`run_full_audit_streaming` path, not a new one.
  - Tagged with trigger reason `policy_change`, extending the existing trigger field, and naming *which* policy area changed, so it's distinguishable in the new history view from a manual re-run or a scheduled interval run.
- **Acceptance for that round**: a real monitored store with multiple past runs shows a real, browsable history (Part 1); a simulated real policy-source change (the same seeded-stale-hash technique already used to test the freshness watcher) triggers a real fresh audit — not just a cache invalidation — for every `on_policy_change` store, with the trigger reason correctly recorded and visible in that same history view (Part 2).

### 6.3 Other open items (unchanged from `final2.md` §14)
- The honest, labeled accuracy validation set — still blocked on 5 real store URLs + human-determined ground truth.
- Per-category product-scan scoping (a discovery-then-scope crawl flow with a picker UI) — requested, not started.
- Cross-audit "memory" feeding a store's own `AuditRun` history back into the LLM checks' own prompts (e.g. "this has been flagged 3 audits running") — discussed, scoped as a small addition on top of the existing DB, not a new memory framework; this project doesn't use LangChain and a chat-style memory abstraction wouldn't fit a single-pass batch pipeline.
