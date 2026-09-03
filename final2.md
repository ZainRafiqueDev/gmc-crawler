# GMC Compliance Checker — Complete System Reference

Everything built, end to end, organized by what it does rather than by when it was built. `final.md` is the round-by-round chronicle (what was found live and why each fix happened, in order); this is the consolidated picture of the system as it stands. A tool that takes a live e-commerce store URL, crawls it, and produces a Google Merchant Center (GMC) policy-compliance report — usable via CLI, a browser frontend, or recurring monitoring.

**Scale**: 52 files in `app/`, 49 test files, 406 automated tests, all passing.

---

## 1. What this tool does, and doesn't claim to do

Give it a store URL. It crawls the storefront with a real browser, classifies every page, runs deterministic and LLM-graded compliance checks against real Google Merchant Center policy text, and produces a report prioritized by what could actually get the account suspended versus what's a lower-priority listing/quality issue. It can also register a store for recurring monitoring and produce delta reports.

It does **not** claim universal crawl coverage — some sites deliberately and successfully block automated traffic, including Google's own crawler at times. When it can't get through, it says so specifically (which failure category, why) rather than fabricating a clean or a missing verdict. This honesty guarantee is treated as load-bearing throughout the design, not an afterthought — see §6.

---

## 2. Core pipeline

`app/graph.py` orchestrates the whole run as a **LangGraph** `StateGraph` (used for flow control only — this project does not use LangChain; the LLM/RAG layer is hand-built, see §5):

```
detect_platform → crawl_and_classify → deterministic_checks → llm_grading → compile_report
```

- **`detect_platform`** (`app/platform_detector.py`) — WordPress/WooCommerce, Shopify, or unknown/custom, via REST API probes (`/wp-json/`, `/wp-json/wc/v3/products`, `/products.json`) with an HTML-marker fallback. Advisory only — every downstream check runs the same way regardless of what's detected; platform-specific enhancements (API-verified product data) just fall back to page-only checks when unavailable. Known limitation: this probe uses a separate `httpx` client (not the full-page crawl), so it can succeed even when the main crawl fails entirely (different client, far lighter request — confirmed expected behavior, not a bug). Known gap (not fixed, out of scope so far): this heuristic can false-positive "woocommerce" on a non-WooCommerce site whose WAF 403s every path uniformly, found live on 3 real stores.
- **`crawl_and_classify`** (`app/site_mapper.py`, `app/fetch.py`) — see §3/§4.
- **`deterministic_checks`** — see §7.1.
- **`llm_grading`** — see §7.2, then `apply_impact_tiers` and `apply_ads_eligibility_impact` run post-hoc over the combined finding set (§8).
- **`compile_report`** — see §9.

`run_audit`/`run_audit_streaming` are the two entry points (the latter reports phase-by-phase progress for the frontend's polling UI). Both wrap the whole run in a per-audit opt-in proxy contextvar scope (§4.5).

---

## 3. Crawling

`app/site_mapper.py` (BFS crawl + classification) and `app/fetch.py` (`PageFetcher` — the resilient per-page fetch, the reliability core the whole project depends on).

### 3.1 Fetch mechanics
- Playwright-driven, JS-rendered (not raw HTML) — a real headless browser, so client-side/SPA content is present.
- Retries with exponential backoff, capped (`max_backoff_seconds`, default 30s).
- Anti-automation-detection hardening: realistic viewport (1366×768, not headless-default-none), locale, `navigator.webdriver` spoofed to `undefined` — some real hosts serve degraded content to a bare headless fingerprint (found live).
- Identifiable `User-Agent` on every request (`gmc-compliance-auditor/0.1`), never spoofed as a real browser — matches robots.txt compliance as the project's transparent-crawler stance (see §11.2 for where that stance was deliberately not crossed).

### 3.2 HTTP-status-aware retry/backoff
Every non-2xx response gets a distinct, named handling path — not one undifferentiated "failed" bucket:

| Status | Behavior |
| --- | --- |
| 404/410 | Confirmed not found, never retried — a real broken link. |
| 429 | Retries with the real `Retry-After` delay when present (capped), exponential fallback otherwise. |
| 401/403 | Capped at `max_bot_block_attempts` (default 2), deliberately lower than the general retry budget — never hammers an identical request against a likely block. |
| 503 / other HTTP errors | Retries like before, now under a named `http_error` category (found and fixed live — this used to fall into a generic `"unknown"` bucket with no distinguishing report language, even though the retry behavior itself was already correct). |
| DNS/connection/timeout | `network_error` — a distinct category from an HTTP response, since no response was ever received. |

A per-domain minimum delay (`Settings.crawl_domain_min_delay_seconds`, default 0.5s, clamped [0,30]) throttles this crawler's own request pace so it doesn't trip a target site's rate limiting — implemented as an async slot-reservation queue (`_DomainThrottle`) so concurrent fetches to the same domain space out correctly rather than bursting.

### 3.3 Anti-bot interstitials and cookie-consent banners
- **JS-challenge detection**: confirmed live that the plain `networkidle` wait does *not* reliably get through a Cloudflare-style "checking your browser" interstitial (it can sit idle for its whole countdown before navigating). An explicit wait-and-recheck step (`_wait_for_challenge_to_resolve`, up to `challenge_wait_seconds`, default 6s) polls content for the interstitial to clear before deciding the page failed. A stale first-navigation status code (often 503 or 403 for a challenge page) is not used to fail a page that demonstrably resolved to real content.
- **CAPTCHA detection** (not solving, by design — see §11.2): a two-tier heuristic distinguishes a genuine full-page CAPTCHA block from a normal page that merely embeds a reCAPTCHA/hCaptcha/Turnstile widget on a form (checked against page-body thinness + explicit "verify you're human" phrasing, not widget presence alone — avoids false-flagging an ordinary contact form).
- **Cookie-consent dismissal**: best-effort click of a recognized "Accept" control (OneTrust, Cookiebot, cookieconsent.js, CookieYes, generic patterns) before content is captured — some EU-facing stores genuinely gate content behind consent. Deliberately conservative (never clicks "reject," never guesses); any failure is swallowed silently, never breaks the fetch.
- Live-validated against a real Cloudflare JS-challenge demo page (the standard public reference for this kind of testing) — fetched successfully end-to-end.

### 3.4 SSRF-guard honesty
Every fetch's SSRF guard stats (`ssrf_requests_validated`/`blocked`) are now accurate even on a failed fetch — two real bugs found and fixed live: (1) `install_ssrf_guard` used to count "validated" only after a full round trip completed, not the moment the destination passed validation, so a request that was legitimately checked but then timed out read as "never validated"; (2) separately, `app/site_mapper.py` silently dropped the stat entirely when building a `CrawledPage` for a failed fetch, so even after fixing (1) the report still showed 0. Live-confirmed against a genuinely unreachable real site: 57 requests validated on a page that never loaded (was 0 before both fixes).

### 3.5 Sitemap discovery and crawl prioritization
- `/sitemap.xml` first (SEO-plugin convention), `/wp-sitemap.xml` fallback (WordPress core's own default, no plugin needed) — some real hosts block one but not the other.
- Two-tier priority seeding: sitemap URLs that look like Store Overview pages (policy/contact) are seeded into the very first wave, ahead of Catalog URLs (products), ahead of homepage-discovered nav links, ahead of everything else — a large-catalog store's category tree can no longer consume the whole page budget before reaching a policy page.
- URL canonicalization (`app/url_canonicalize.py`) strips WooCommerce-style filter/sort facet query params before a URL becomes the crawl's dedup key — roughly half of a real 218-page crawl was facet-query duplicates before this fix.

### 3.6 Internationalization
Audited (not assumed): the page classifier's text rules were English-only; URL rules were only partially language-agnostic. Live-confirmed the real gap against a real French WooCommerce store (`meo.fr`, found specifically to get a genuine non-English test case) — `/mentions-legales`, `/categorie-produit/...`, `/produit/...` (WooCommerce's own French-locale defaults) all fell through to `BLOG_OTHER` before the fix.

- Real URL + text pattern coverage added for Spanish, French, German, Portuguese, Italian across every required-page type *and* product/collection types (`SUPPORTED_LANGUAGES = {en, es, fr, de, pt, it}` — the explicit, honest boundary, not claimed complete).
- **The honesty backstop**: `CrawledPage.detected_language` (from `<html lang>`) feeds a downgrade in `check_required_pages` — a "missing" verdict for a store whose dominant language isn't in `SUPPORTED_LANGUAGES` is downgraded from `CONFIRMED`/`CRITICAL` to `CANNOT_VERIFY`/`MEDIUM`, stating the classifier can't fully read this site's language rather than asserting a false negative. A store simply not declaring `<html lang>` is *not* treated as unsupported (would blunt the check for ordinary English sites).
- Separate honest observation from the live test: some small EU stores fold shipping/returns content into one consolidated terms page rather than separate pages (a legal-convention difference, not a language gap) — noted, not "fixed" (would need checking content-within-a-page, a deeper redesign, not attempted).

### 3.7 Opt-in BYO proxy support
Deliberately **not** built until live evidence justified it. After Parts 1–4 of the crawl-robustness round, live testing found concrete IP/fingerprint-level blocking on real major-brand sites — `curl` got a clean HTTP 403 from one while headless Chromium got a connection-level failure from the same source IP; another was reachable via `curl` but not via Chromium at all. Brought back to the user as an explicit decision (Part 5.2's own framing: this crosses from "resilient crawler" into "defeating a site's anti-automation defenses," so it shouldn't be built silently) — the user chose to build it.

- `app/proxy_config.py` — off by default (empty settings = zero behavior change). BYO-proxy only: this project doesn't operate or bundle any proxy infrastructure, it lets an operator point the tool at their own legitimate subscription, the same way robots.txt compliance and an honest User-Agent are already built in rather than spoofed.
- Two shapes: a single rotating-residential gateway (`PROXY_SERVER`/`PROXY_USERNAME`/`PROXY_PASSWORD` — the provider rotates the exit IP server-side per connection, and a fresh Playwright context is already opened per fetch attempt, so this rotates for free) or a client-side round-robin pool of distinct static endpoints (`PROXY_POOL`).
- Wired into both traffic paths (Playwright's `PageFetcher.proxy_rotator`, httpx's `contextvars`-based `set_current_proxy`/`safe_async_client` — a `ContextVar`, not a plain global, so concurrent audits in the same process never race each other's setting).
- **The one non-negotiable**: the SSRF guard stays fully active regardless of proxy configuration — the proxy is configured directly on `SSRFSafeTransport` itself, not left to httpx's separate proxy-mount mechanism, so the same transport instance that always validates the destination is the one that then makes the (possibly-proxied) connection. Directly tested.

---

## 4. Page classification

`app/page_classifier.py` — URL patterns first (language-covered per §3.6), then heading text, then body text, in that priority order, first match wins.

- `/cart`, `/checkout`, `/my-account`, `/login`, `/register` classified explicitly ahead of text rules — WooCommerce's default account page renders a stock privacy-policy consent sentence that the text fallback used to misclassify the page AS the privacy policy (found live, fixed).
- **A page with its own distinct, non-matching heading no longer falls through to the body-text-wide search** — found live (a real store's "Legal Notice" page, which merely referenced "our privacy policy" in a sentence about data handling, was misclassified as the privacy policy itself; "Payment Policy" similarly misclassified as `checkout`). The fix is deliberately conservative: an honest `BLOG_OTHER` is safer than a wrong specific-type guess, since a wrong tag can silently satisfy `check_required_pages` and suppress a real "missing page" finding on a store where no other page of that type exists.
- Crawl-priority tiers (`looks_like_overview_priority_url`/`looks_like_catalog_priority_url`) feed §3.5's seeding.

---

## 5. LLM layer — the grounding mechanism

**Not LangChain.** `app/llm/client.py` defines a plain `Protocol` (`call_tool`, `call_tool_with_image`) implemented separately for Claude (forced tool-use) and OpenAI (Structured Outputs) — `app/llm/factory.py` picks one based on `Settings.llm_provider`. Every implementation forces the model to return a schema-conforming structured result; free-form text is never accepted as a check's answer.

### 5.1 Real RAG policy index (Phase C)
`app/llm/policy_rag.py` — genuine retrieval, not a canned prompt:
- **Sources**: 8 policy areas (`app/policy_watcher.py::POLICY_SOURCE_URLS`), each pointing at real, confirmed `support.google.com/merchants/...` pages.
- **Pipeline**: heading-based chunking → OpenAI `text-embedding-3-small` embeddings (content-hash cached) → SQLite storage (`PolicyChunk`) → Python-side cosine-similarity retrieval, deduped to one chunk per (source_url, section).
- **Dynamic retrieval**: the query embedded per check is the actual page text being graded, so two different pages checked against the same policy can surface different top-N chunks.
- **Freshness watcher** (`app/policy_watcher.py::check_policy_sources`) — hash-diffs each policy area's real source page(s) independently of any store's schedule; a real change triggers a full re-chunk/re-embed and invalidates the entire LLM result cache.
- **Citation freshness surfaced in the report**: `PolicyContext.verified_at` (the oldest `PolicyChunk.created_at` among the chunks actually cited — the conservative choice when a citation spans chunks verified at different times) is threaded to `Finding.policy_last_verified` and rendered as "Official Source: [url] (last verified: [date])." Live-confirmed with a real date, not a placeholder.
- Falls back to a hand-written stub snippet set (`app/llm/policy_snippets.py`) only if retrieval genuinely can't run (no index yet, no DB, no key, or a live embedding-call failure) — `PolicyContext.from_real_index` marks which happened, never silently indistinguishable.

### 5.2 Anti-hallucination design
- Every finding-producing check requires a **verbatim** `evidence_quote` field copied from the actual page text — the system prompt explicitly forbids inventing, paraphrasing, or reconstructing a quote from memory; an empty string is required when nothing relevant exists, rather than a fabricated one.
- `Finding.policy_requirement_text` preserves the real retrieved RAG text a finding was graded against (not just the citation string) — rendered as "Specific Policy Requirement" in **every** finding, not just the headline suspension-risk ones (found live and fixed: a real quality-tier LLM finding with genuine RAG grounding was silently losing that grounding the moment it landed in "Other Findings" instead of "Suspension Risk," because only the richer renderer read the field).

### 5.3 The four LLM-graded checks (`app/llm/checks.py`)
1. **`check_policy_page_substance`** (`llm_policy_substance_*`, one per required policy type) — does a policy page that exists actually say anything substantive, not just exist. Grounded suspension-risk (a page that exists but is empty is functionally near-equivalent to missing).
2. **`check_editorial_quality`** (`llm_editorial_quality`) — spelling/grammar, placeholder text ("Lorem ipsum"), auto-generated-reading content. Quality-improvement tier (no suspension language found in the real retrieved text for this).
3. **`check_prohibited_content`** (`llm_prohibited_content`) — screens for prohibited/restricted product categories. Suspension-risk tier (explicit escalation-to-suspension language in the real retrieved policy text). Includes counterfeit/brand-risk guidance folded into the existing prompt (not a new check): watch-terms (replica, 1:1, mirror quality, AAA-as-quality-grade, knockoff) plus an explicit rule that a brand name alone is never evidence of counterfeiting. Live-confirmed with two real model calls: "compatible with iPhone 14... works with Apple MagSafe" → no finding; "AAA Quality 1:1 Mirror Replica of the original Rolex Submariner" → correctly flagged, CRITICAL, real quoted evidence.
4. **`check_claim_policy_contradiction`** (`llm_claim_policy_contradiction`, new) — a marketing claim on a product/homepage page ("free shipping," "30-day returns") versus what the store's own shipping/returns policy page actually says. Confirmed genuinely uncovered before building (the business-identity check only cross-checks contact fields; the price/stock checks compare product data against a platform API, never policy-page text).
   - Cheap regex pre-filter before any LLM call (only pages that actually mention a shipping/returns-adjacent claim get sent).
   - Topic-locked per claim type (shipping vs. returns evaluated separately, against the matching policy page) — found live that without this, the model could quote a shipping claim as "contradicting" a returns policy page.
   - Structured `conflict_dimension` field (cost / timeframe / eligibility / availability / none), not a bare yes-no — forces the model to name what specifically conflicts.
   - **Real false positives found and fixed via live testing**: an early version flagged "30-Day Free Returns" against a policy's own ordinary exceptions clause (used/damaged items) for the *same* 30-day window, and separately flagged "free shipping" against a policy that literally agreed with it. Prompt hardening with explicit negative examples was tried first and was *not sufficient alone* — reproduced on 5 of 5 real product pages on a real store even after that fix. A deterministic backstop was added: when the model claims a `timeframe_or_window` conflict but both quotes name the identical day count, the finding is discarded regardless of what the model concluded. Re-verified: 0 false positives on two real, previously-clean stores; a genuine synthetic mismatch was still correctly caught with real quotes on both sides.
   - Grounded suspension-risk via `misrepresentation` (same pattern as business-identity inconsistency — a claim contradicting stated policy is the same category of issue).
   - Unlike the other three checks, a failed/empty LLM call produces no finding at all here (not a CANNOT_VERIFY placeholder) — this check is opportunistic on top of an already-thin pre-filter match, not a required-page-type check where "couldn't check" is itself worth surfacing.

Tiered by design: LLM grading only runs on page types where it adds real judgment value (policy pages, homepage, product pages) — collection/cart/checkout/contact/FAQ/blog pages are never sent to the LLM, kept cheap (~15 fresh calls on a real 200-page crawl in early testing).

`app/llm/image_checks.py` (vision, Phase B) — mismatch detection and a prohibited-content screening flag on product images, both graded via a forced tool-use schema requiring a `location` field. **Confirmed live**: vision checks use the older `policy_snippets.py` stub system, not the real RAG index (`app.impact_tier.py` already documents this explicitly as "no direct textual grounding found," a known, deliberate, pre-existing gap — not a regression, and not fixed this round; flagged as a possible future decision, not built silently).

---

## 6. Honest-failure reporting — treated as load-bearing throughout

The single most-revisited design principle across every round: **never assert a confident conclusion from an incomplete picture.**

- `SiteMap.crawl_totally_failed` (true when nothing was ever reachable, homepage included) gates `check_required_pages` and `check_business_identity_consistency` — a total failure produces one honest `CANNOT_VERIFY` finding stating why, not up to 6 confident false-negative "Missing"/"No contact info" findings. Live-confirmed: 6 of 13 real user-supplied stores hit exactly this path in one test batch, correctly, with zero false claims.
- `check_broken_internal_links` doesn't *also* re-report the same total-failure fact per unreachable page (found live: a real report showed 3 "hidden" lower-priority findings that were really one fact restated three ways by three different checks) — fixed to suppress only the redundant CANNOT_VERIFY case; a confirmed 404 is untouched (a genuinely distinct fact).
- **The report-level banner**: `## This Audit Could Not Run` shown prominently (even under `major_only=True` — a total failure is not a lower-priority finding to hide), stating the specific failure category and, where actionable, what to do (e.g. ask the merchant to allowlist the tool's User-Agent).
- **The score-contradiction bug class, found and fixed twice, then closed at the root**: three separate report sections (Final Assessment, Policy-by-Policy matrix, and — found in a later round — "At a Glance") each had their own duplicated `crawl_totally_failed` check; fixing two of three left the third showing a confident "100/100 (LOW risk)" right next to the "could not run" banner. Root-caused properly: `compute_risk_score` itself now returns `RiskScore.not_applicable` when the crawl failed, and every renderer reads that one field — a new display site can no longer reintroduce the gap by forgetting its own separate check.
- The WooCommerce-API-connection recommendation is similarly suppressed on a total failure (platform detection is a separate, unreliable-in-this-state probe).
- Every unreachable page's report entry states its specific failure category (`network_error`, `bot_blocked`, `captcha_blocked`, `rate_limited`, `http_error`, `not_found`, `blocked_ssrf`) with a category-specific recommended action — not a generic "could not verify."
- Score-breakdown honesty: the raw pre-clamp score is shown explicitly when clamping actually changes the displayed value ("Raw score before clamping: -195 - floored to 0"), and CANNOT_VERIFY findings excluded from scoring are stated explicitly ("N finding(s) excluded from scoring: cannot-verify") — both found live on a real report that didn't explain either gap.

---

## 7. Checks — full list

### 7.1 Deterministic (`app/checks/*.py`, no LLM call)
- `check_https` — homepage HTTPS enforcement, mixed-content internal links.
- `check_required_pages` — presence of privacy/shipping/returns/terms/contact pages, with the total-failure and language-downgrade honesty gates from §3.6/§6.
- `check_external_links` — flags every external-domain link (per-instance, low severity — deliberately not suspension-tier, since it fires on entirely benign social-media icons too).
- `check_duplicate_nav_footer` — 3+ near-identical nav/footer blocks on one page (2 is normal responsive-theme behavior, not flagged).
- `check_broken_internal_links` / `check_broken_images` — confirmed-404 vs. cannot-verify, distinct always; the redundancy fix from §6.
- `check_business_identity_consistency` (`app/checks/business_identity.py`) — cross-page email/phone/address consistency, phone-country-code-vs-stated-address mismatch. The total-failure honesty gate applies here too.
- `check_forms` (`app/checks/form_checks.py`) — structural checks on every contact/newsletter form (missing fields, unreachable submission endpoint), never submits a real form (read-only parse + at most a GET, never POST). **WooCommerce's native product-review/comment form was found live to be misdetected as a broken contact form** — its "Name" field's own attribute is literally `author` (not "name"), and its `<label>` is a DOM sibling, not a parent, of the input, so the generic name-detection heuristic never recognized it; its submission endpoint (`wp-comments-post.php`) legitimately rejects a plain GET (POST-only by WordPress core design). Produced 10 false suspension-risk findings across 5 product pages on one real store. Fixed with the same exclusion pattern already used for login/register forms — a reliable id/class/action signature checked before any classification runs. Blast-radius checked explicitly: 2 other previously-tested WooCommerce stores didn't currently render this form (reviews disabled at the theme/store level), so their past reports weren't affected — a per-store configuration fact, not a code guarantee; the underlying gap existed for the check's entire history and affects any WooCommerce store with reviews enabled.
- `check_duplicate_products` (`app/checks/duplicate_products.py`) — exact and near-duplicate (≥0.92 similarity) product titles across distinct URLs.
- Product-data checks (`app/checks/generic_product.py`, `woocommerce_products.py`, `shopify_products.py`, dispatched via `app/checks/product_checks.py`) — price/stock/availability, API-verified against the WooCommerce REST API or Shopify's public `products.json` when credentials/availability allow, page-only scraping otherwise — the verification method is always labeled in the report, and a prominent recommendation appears when a WooCommerce API route was detected but couldn't be authenticated against.
- `app/checks/product_images.py` — broken links, missing alt text, low resolution, placeholder-filename detection (deterministic half of Phase B).
- `app/checks/purchase_journey.py` (opt-in, off by default) — add-to-cart → checkout walkthrough, structurally incapable of clicking any payment/place-order control (not just "stops before" — no code path goes further). Built and unit-tested; never validated against a live store's real checkout (no test store with checkout access available) — documented as an open gap, not claimed proven.

### 7.2 LLM-graded — see §5.3.

---

## 8. Classification layers applied post-hoc

Both computed once per finding set, non-mutating (return new `Finding` objects — the input list is shared elsewhere in the pipeline, e.g. delta diffing):

- **`ImpactTier`** (`app/impact_tier.py::apply_impact_tiers`) — `suspension_risk` / `listing_disapproval` / `quality_improvement`, grounded per check_id in real retrieved GMC policy text (quoted in the module itself, traceable). Defaults to the cautious `listing_disapproval` for any check_id without a confident grounding (`AMBIGUOUS_CHECK_IDS` names which ones explicitly, rather than guessing they're harmless).
- **`AdsEligibilityImpact`** (`app/ads_eligibility.py::apply_ads_eligibility_impact`, new) — `ads_and_listings` / `listings_only` / `unclear`. Real research (live-fetched this session): Google's "Free listings policies" page mirrors the Shopping ads policy category-for-category, and "Free listings for products" explicitly requires shipping settings and return-policy info for free-listing eligibility, not just ads. Every one of the 8 tracked policy areas is grounded `ads_and_listings` — a genuine research conclusion (the overwhelming real-world pattern is joint enforcement), not a forced 3-way split or a lazy default; `listings_only` exists as a value with no current check_id grounded there, `unclear` covers checks with no policy-area attribution at all (crawl-hygiene, not real "policy compliance" findings). Reuses `policy_area_for_finding` rather than a second parallel mapping.

Both feed the report's tier-based sectioning and the client's original stated use case (pre-ads compliance, not just general GMC compliance).

---

## 9. Report structure (`app/report.py`)

1. **Header** + prominent API-verification recommendation when relevant (§7.1).
2. **`This Audit Could Not Run` banner** when applicable (§6) — the only section that can override everything below it.
3. **Prose Executive Summary** — suspension-risk count, critical count, ads-eligibility-affecting count (§1), overall risk rating.
4. **At a Glance** — platform, crawl stats (with a could-not-verify breakdown by category, §6), SSRF guard stats (§3.4), cache hit rate, the Internal Audit Score with its full itemized, reproducible breakdown (§6).
5. **Suspension Risk Findings** (primary section) — every Critical finding plus every confirmed suspension-tier finding, in the rich per-finding format: Location, Relevant Policy, Specific Policy Requirement (real RAG text), Why It Matters, Ads Eligibility Impact, Recommended Fix, Official Source (with freshness date, §5.1).
6. **Policy-by-Policy Review** — one row per tracked policy area, confidence-aware status (`Fail`/`At Risk`/`Cannot Verify`/`Pass` — reuses `Confidence` directly; found live that a blanket "At Risk" was hiding the difference between one confirmed critical issue and 80 potential-risk external links).
7. **Other Findings (Lower Priority)** — grouped by tier, same rich fields as the suspension section (§5.2's fix), never silently dropped, just deprioritized.
8. **Page-by-Page Findings** — Store Overview (one entry per page) then a grouped Catalog Overview (canonical-URL-grouped, §3.5) — each unreachable page states its specific failure reason (§6).
9. **Required Fixes (Prioritized)** — strictly the same predicate as the Suspension Risk section, not a separate severity check (a past bug: these two sections used different logic and could contradict each other).
10. **Final Assessment** — overall rating, single most important next action, an honest limitations note.

`major_only=True` renders the suspension-risk-focused view only, with an explicit count of what's hidden — never a silent drop. `generate_delta_report` compares two runs' findings by `(check_id, page_url)` identity for New/Resolved/Changed/Unchanged. Markdown is canonical; Word (`report_docx.py`) and PDF (`report_pdf.py`) reuse the same markdown-parsing structure, including real table rendering for the Policy Matrix. Every scraped-content field is HTML-escaped and control-character-stripped before rendering (`app/security/sanitize.py`) — verified against real `<script>`/`<img onerror>` payloads.

---

## 10. Security hardening — unconditional, not a togglable layer

`app/security/ssrf_guard.py` — three layers active on **every** fetch, no toggle, no fallback path that skips any of them:
1. Upfront validation of the user-supplied URL before any work starts.
2. Per-request re-validation before dispatch (a custom httpx transport; a Playwright `context.route()` guard) — protects against DNS rebinding, since a URL that resolves public at input time can resolve internal moments later.
3. A post-navigation check of the actual final URL — necessary because Chromium doesn't expose intermediate redirect-chain hops as separately interceptable requests (verified live).

Blocks private/loopback/link-local/reserved/multicast ranges including the cloud metadata endpoint (169.254.169.254). Every successful fetch reports positive, countable confirmation the guard ran, not just an absence of violations. `DNSResolutionError` is a distinct, retried exception separate from a genuine blocked-IP verdict (found live: a transient DNS hiccup was once causing real, existing policy pages to report as "confirmed missing" instead of "could not verify" — the origin bug the whole §6 honesty discipline traces back to). The proxy-support addition (§3.7) was built specifically to never weaken this — the guard runs on the same transport that then makes the connection, proxied or not.

Also: rate limiting on the API's audit-creation endpoint (default 5/hour/IP), hard resource ceilings (pages/depth/concurrency/timeout/image size, enforced by validators no caller can bypass), robots.txt compliance (fail-open if unreachable, hard-coded skip for cart-mutating action URLs regardless), no secrets ever logged.

---

## 11. Known gaps and deliberate non-features

### 11.1 Documented, not hidden
- Purchase-journey checks (§7.1) never validated against a live checkout.
- No alerting webhook (deferred, pending the user's own notification tooling).
- Frontend has no auth — fine for local/internal use, not for open-internet exposure as-is.
- A WAF TLS-fingerprinting issue on at least one real store causes false "broken image" positives via httpx (would need a TLS-fingerprint-spoofing client — separate scope; the proxy support in §3.7 can sidestep some IP-based instances of this but doesn't fix the TLS-fingerprint issue itself).
- No detection of a contact page with *no* form at all (only checks forms that exist).
- `AMBIGUOUS_CHECK_IDS` (§8) — generic broken-link/HTTPS/image-mechanics checks with no direct policy-text grounding, defaulted cautiously and flagged as such, not guessed.
- Vision checks (§5.3) aren't RAG-grounded, unlike the four text-based LLM checks — a known, pre-existing, documented gap.
- `platform_detector.py`'s false-positive risk on a uniformly-403ing WAF (§2) — found live, not fixed (out of the scope it was found in).
- A live-observed `sqlite3.IntegrityError` (embedding-cache concurrent-write race) — degrades gracefully (falls back to the stub snippet for that one call), not investigated further.

### 11.2 The ethical line, held deliberately
Two things would further raise real-world success against sites that deliberately block automated traffic, and both were treated as decisions, not defaults:
- **CAPTCHA solving** — never built. Detected and reported only (§3.3), with an explicit "this tool never attempts to solve CAPTCHAs, by design" message and a recommendation to ask the merchant to allowlist the tool or do a manual check.
- **Residential/rotating-proxy IP evasion** — flagged first with concrete live evidence before building (§3.7), built only once the user made that call explicitly. The line drawn throughout: robots.txt compliance and an honest, identifiable User-Agent are already built in rather than spoofed: this project stays a transparent, identifiable crawler, not one that silently defeats a site's own anti-automation defenses.

---

## 12. Monitoring, frontend, API

- **Monitoring** (`app/monitor_service.py`, `app/db.py`, `monitor.py`) — register a store for `interval` (scheduled re-audits), `on_change` (cheap hash-diff triggers a full audit only when policy pages actually change), or both. Independent of `app/policy_watcher.py`'s own GMC-policy-source watch schedule (§5.1).
- **API** (`app/api/main.py`, FastAPI) — job-based audit creation with live phase polling (`run_audit_streaming`), Markdown/docx/PDF downloads, monitoring CRUD. Job/report persistence in the DB, not process memory — survives a backend restart mid-audit (verified live). **Windows-specific gotcha, documented in the module docstring**: don't run with `uvicorn --reload` — its reload mode forces a `SelectorEventLoop` for the worker subprocess on Windows, and `SelectorEventLoop` can't itself spawn subprocesses, which is exactly what Playwright's browser launch needs at startup. Plain `uvicorn app.api.main:app --port 8010` (no `--reload`) works correctly; confirmed live.
- **Frontend** (`frontend/`, Next.js) — Home (run audit), Report (phase-progress polling → rendered report → downloads → register-for-monitoring), Monitored Stores, Store Report screens. No auth (§11.1).

---

## 13. Testing and real-world validation

**406 automated tests**, all passing, covering every module above — the SSRF guard (including the proxy-still-validates guarantee), rate limiter, caching, sanitization, robots handling, config clamping, every check module, both LLM providers, the RAG pipeline, docx/PDF export, URL canonicalization, job persistence, the full report structure, i18n classification, the ads-eligibility/impact-tier grounding, and the claim-contradiction check's false-positive regression cases specifically.

**This is not just unit-tested.** Real, live stores used across this project's history, with real bugs found and fixed from actually reading the output, not assumed correct:

| Store | Platform | What it validated / found |
| --- | --- | --- |
| britanniagifts.us | WooCommerce | Original real-store validation baseline; bot-detection/viewport fix, SSRF-guard CDP-corruption fix, WAF TLS-fingerprint false-positive (documented, not fixed) |
| iana.org | — | Report-rendering stress test at scale (150 pages, 1273 findings) |
| meo.fr | WooCommerce (French) | i18n classification fix; claim-contradiction false-positive-free confirmation (60-page crawl, 0 false positives) |
| allbirds.com | Shopify | Real bot-blocked interstitials on `/collections/*` pages, correctly categorized and capped |
| velasca.com, snocks.com | Shopify | Crawl-budget/timeout edge cases, per-domain politeness confirmed |
| vellano.site | WooCommerce | The review-form false-positive (10 false findings → 0), the `/shipment-policy` classification gap, the claim-contradiction false positives found and fixed live, ads-eligibility count confirmed (69 real findings tagged) |
| byredo.com | — | robots.txt-disallowed path, correctly reported as a respectful refusal, not a failure |
| nike.com, decathlon.fr, zalando.fr, tiendanimal.es, hellyhansen.com, solostove.com, cutterbuck.com | — | Real total-crawl-failure paths (network-level and bot-blocked), zero false "missing page" claims across all of them — direct evidence for the proxy-support decision (§3.7) |
| nowsecure.nl | — | Standard public Cloudflare-JS-challenge reference page — fetched successfully end-to-end |

A 13-site batch specifically (see `test.md`) found 3 more report-honesty bugs (score/policy-matrix/API-recommendation contradictions on a total-failure report) purely from reading real generated output — none of which any unit test alone would have surfaced, since the bug was about *coordination* between independently-correct sections.

Total LLM API spend across every real-store run to date: a handful of cheap calls plus a one-time ~$0.0004 embedding build — well under $0.05 all in.

---

## 14. In progress / open, not started

- **Honest, labeled accuracy validation set** (`validation/`) — the checklist template and per-category precision/recall scoring script are built. Still blocked on 5 real, authorized store URLs and human-determined ground truth (not LLM-generated, per the original constraint).
- **Per-category product-scan scoping** (discovery-then-scope crawl flow, a picker UI for which categories/products to audit) — requested, not started.
- **Cross-audit "memory" for the LLM checks** — discussed, not built: the DB already has full audit history (`AuditRun`) and produces delta reports, but that history isn't currently fed back into any LLM check's own prompt (e.g. "this has been flagged 3 audits running"). Scoped as a small, targeted addition on top of the existing DB if wanted — not a new memory framework; this project doesn't use LangChain and a chat-style memory abstraction wouldn't fit a single-pass batch pipeline.
