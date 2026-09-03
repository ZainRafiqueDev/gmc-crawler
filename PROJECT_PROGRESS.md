# GMC Compliance Checker — Build Progress

A tool that takes a live e-commerce store URL, crawls it, and produces a Google Merchant Center (GMC) policy-compliance report — usable via CLI or a browser frontend, with optional recurring monitoring. Built from scratch across multiple sessions. This document summarizes what exists, how it was validated, and what's still open.

---

## 1. Core pipeline

**Crawl → classify → check → report**, orchestrated as a LangGraph state machine (`app/graph.py`): `detect_platform → crawl_and_classify → deterministic_checks → llm_grading → compile_report`.

- **Platform detection** (`app/platform_detector.py`) — identifies WordPress/WooCommerce or Shopify via REST API probes, falls back to generic/unknown. Framework-agnostic by design: checks operate on rendered DOM/text, not detected platform, so custom MERN/MEVN/Java/Python-built stores work identically.
- **Crawling** (`app/site_mapper.py`, `app/fetch.py`) — Playwright-driven, JS-rendered fetches (not raw HTML), with:
  - Resilient retry logic with exponential backoff, `CANNOT_VERIFY` status after exhausting attempts (never silently treated as "broken").
  - Sitemap discovery: tries `/sitemap.xml` first, falls back to `/wp-sitemap.xml` (WordPress core's own default sitemap, no SEO plugin required).
  - Crawl-priority seeding: product/policy-looking URLs are pushed into the first BFS wave so a large-catalog store's category tree doesn't consume the entire page budget before reaching a real product or policy page.
  - robots.txt compliance (fail-open if unreachable, per convention), plus a hard-coded skip for known cart-mutating action URLs (`?add-to-cart=`, etc.) regardless of whether robots.txt itself was fetchable.
  - Anti-automation-detection hardening: realistic viewport, locale, and `navigator.webdriver` spoofing — some real hosts' bot-mitigation silently serves degraded content to a bare headless-browser fingerprint (found and fixed on a real store this session).
- **Page classification** (`app/page_classifier.py`) — tags each crawled page by type (product, privacy policy, shipping policy, returns policy, contact info, FAQ, etc.).

## 2. Checks

- **Deterministic checks** (`app/checks/deterministic.py`, `business_identity.py`, `woocommerce_products.py`, `shopify_products.py`, `generic_product.py`) — required-page presence, business identity/contact consistency, product data completeness. Verified against platform APIs (WooCommerce REST, Shopify) where available; best-effort page-scraping otherwise, with the verification method always labeled in the report.
- **Product image checks** (`app/checks/product_images.py`) — deterministic (broken links, missing alt text, size/format) plus vision-graded checks (watermarks, placeholder images, image/description mismatch). Images deduped by URL so a shared image is graded once, not once per page.
- **LLM-graded checks** (`app/llm/checks.py`) — judgment-based policy compliance (misleading claims, missing disclosures, editorial/professional quality, privacy-policy substance) using forced-schema tool calls so the model cannot produce a finding without citing verbatim evidence from the page.
- **Purchase-journey verification** (`app/checks/purchase_journey.py`) — opt-in, safety-critical add-to-cart → checkout walkthrough. Never submits real payment. Built and unit-tested; **not yet validated against a live store** (no test store available with real checkout access) — documented honestly as an open gap, not claimed as proven.

## 3. LLM layer — provider-agnostic, proven anti-hallucination guarantee on both

- `app/llm/client.py` defines a `Protocol` implemented separately for **Claude** (`claude_client.py`, forced tool-use) and **OpenAI** (`openai_client.py`, Responses API with strict Structured Outputs). Switchable via `LLM_PROVIDER=claude|openai`; current default in use is OpenAI (`gpt-4o-mini`).
- `app/llm/cache.py` — content-hash-keyed cache wrapping either provider transparently, backed by the DB. Measured real savings: a repeat audit of an unchanged site showed a 100% cache hit rate and a corresponding wall-clock time drop.
- Tiered checking: LLM/vision grading is deliberately skipped on low-value page types, keeping real-world runs cheap (a 200-page real-store crawl used 15 fresh LLM/vision calls total, all `gpt-4o-mini`).

## 4. Findings & reporting

- Every `Finding` carries a real `detected_at` timestamp (set at creation) and a `location` — a CSS selector for deterministic checks, a **required structured-output field** (not free text) for LLM/vision checks.
- Report format is exact and consistent: `[SEVERITY] Title — location — detected TIMESTAMP UTC`, with all scraped-content fields (evidence, titles, etc.) HTML-escaped and control-character-stripped before rendering, so a malicious page's own content can't inject anything into the generated report.
- Exports: Markdown (`app/report.py`) and Word/.docx (`app/report_docx.py`). PDF is not built.

## 5. Monitoring subsystem

- `app/monitor_service.py` + `app/db.py` — register a store for `interval` (scheduled full re-audits), `on_change` (cheap hash-diff checks that trigger a full audit only when policy pages actually change), or both.
- `app/policy_watcher.py` — independently tracks the real GMC policy Help Center pages via hash-diff, on its own schedule, separate from any store's.
- `monitor.py` — CLI to register/list/remove stores and run the scheduler as a long-lived service.

## 6. Security hardening — treated as blocking, not optional, before frontend exposure

- **SSRF protection** (`app/security/ssrf_guard.py`) — three layers active on **every** fetch, unconditionally, no toggle, no fallback path that skips any of them:
  1. Upfront validation of the user-supplied URL before any work starts.
  2. Per-request re-validation before dispatch (custom httpx transport for every httpx call; a Playwright `context.route()` guard for every browser request).
  3. A post-navigation check of the actual final URL — necessary because Chromium does not expose intermediate redirect-chain hops as separately interceptable requests (verified live; a real platform limitation, not a code gap).
  
  Blocks private/loopback/link-local/reserved/multicast ranges including the cloud metadata endpoint (`169.254.169.254`). Every successful fetch reports **positive, countable confirmation** the guard ran (`ssrf_requests_validated`/`blocked`), not just an absence of violations — surfaced directly in the report's executive summary.

  **A real bug was found and fixed here mid-project**: the Playwright guard's original implementation (`route.continue_()`) was found to corrupt a real WooCommerce store's response (truncated to a body-less document) due to a Chromium/CDP interaction quirk — completely unrelated to the validation logic itself. Fixed by switching to `route.fetch()` + `route.fulfill()`, verified across repeated live runs plus a dedicated test where a page's own attempt to reach the cloud metadata endpoint was aborted with zero bytes sent.

- **Rate limiting** — sliding-window limiter wired into the audit-creation endpoint (default 5 requests/hour per IP).
- **Hard resource ceilings** — pages, depth, concurrency, timeout, image size — enforced by validators that can't be bypassed by a CLI flag, API request body, or a later in-process attribute assignment.
- **Report sanitization** — every field that can contain scraped (possibly malicious) content is escaped before rendering.
- **robots.txt + identifiable User-Agent** on every outbound request.
- No secrets ever logged; `.env` gitignored, `.env.example` kept blank (a real leaked key was caught and scrubbed during development).

## 7. Frontend

- FastAPI backend (`app/api/main.py`) — job-based audit creation with live phase polling, Markdown/docx download, monitoring CRUD.
- Next.js app (`frontend/`) — four screens: **Home** (run audit, configurable page budget), **Report** (phase-progress polling → rendered report → downloads → register-for-monitoring), **Monitored Stores** (list, mode/interval, link to latest report), **Store Report** (a monitored store's latest completed report).
- **Job/report persistence**: ad-hoc audit jobs and monitored-store reports are stored in the database, not process memory. Verified live — killed the backend process mid-session (twice, including once mid-audit) and confirmed completed jobs' status, findings, and both downloads survive a restart with no re-run required; a job that was actively running at the moment of a kill correctly comes back as an "interrupted" error rather than hanging forever as "running." Retention policy is explicit and enforced: monitored-store history keeps the last 10 full audits per store; ad-hoc jobs are pruned after 30 days — both configurable.
- No auth, no styling polish, no history-browsing UI (the data persists; there's just no screen to browse older runs yet) — matches the agreed scope.

## 8. Real-world validation

This is not just unit-tested — it has been run against real, live stores end-to-end through the actual browser frontend, no CLI:

- **iana.org** — stress-tested report rendering at scale (150 pages, 1273 findings); caught and fixed a real renderer freeze on very large reports.
- **britanniagifts.us** (real, live WooCommerce store) — the most thorough real-world run: 200 pages crawled, 1217 findings, real product-page findings (missing-availability checks, missing alt text, LLM-graded description quality citing real scraped prices, vision checks catching a lazy-load placeholder image being fed to the vision model), real policy-page findings (a genuine "privacy policy lacks required substance" finding), and a full register-for-monitoring flow completed in-browser.
  - Along the way, found and fixed: the bot-detection/viewport issue, the SSRF guard's CDP corruption bug, and confirmed (not fixed — a deeper WAF TLS-fingerprinting issue affecting our HTTP client, out of scope for now) that a small number of "image broken (403)" findings on this specific site are false positives caused by our own tooling being blocked, not real broken images. Reported honestly rather than left unexplained.
- Total OpenAI spend across every real-store run this session: a handful of cheap `gpt-4o-mini` calls, well under $0.05.

## 9. Testing

192 automated tests (pytest) covering the SSRF guard (including handler-level tests that would catch a regression back to the corrupting `route.continue_()` implementation), rate limiter, caching, sanitization, robots handling, config clamping, every check module, both LLM providers, docx export, and job persistence semantics. Frontend passes `next build` / `eslint` clean.

## 10. Known gaps — documented, not hidden

- **No real pgvector RAG policy index yet** — LLM-graded checks currently cite a stub policy-summary file, not live-retrieved chunks of the real Google Merchant Center Help Center pages. This is the next planned piece of work (Phase C): scrape the real policy pages, chunk them, embed with OpenAI's `text-embedding-3-small`, store in pgvector/SQLite, and retrieve top-N relevant chunks per check with real source-URL citation. **Not started yet** — deliberately paused to first (a) prove the crawler works on a real ecommerce store, which is done, and (b) build an honest, labeled accuracy benchmark (below), per the current working agreement.
- **Purchase-journey checks** — built, unit-tested, never run against a real store's live checkout.
- **No alerting webhook** — deferred pending the user's existing notification tooling elsewhere.
- **Frontend has no auth** — fine for local/internal use, not for open internet exposure as-is.
- **A specific WAF TLS-fingerprinting issue** on at least one real tested store causes our httpx-based image-integrity checks to report false "broken" positives; not fixed (would require a TLS-fingerprint-spoofing HTTP client, separate scope).

## 11. In progress right now

Building an **honest, labeled accuracy validation set**: 5 real, authorized stores (mixed platforms) with independently-determined manual ground truth per check category, compared against the tool's actual output to produce per-category precision/recall — explicitly *not* a single blended "X% accurate" marketing number, and explicit about any category too thin in the sample to say anything meaningful about yet.
