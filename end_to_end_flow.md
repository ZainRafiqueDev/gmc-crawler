# End-to-End Flow — The GMC Compliance Checker, Built From Scratch

A single, continuous walkthrough of the whole system: what it is, the full journey one audit takes
from request to delivered report, and why each stage works the way it does. Where `execution_flow.md`
is a terse call-graph reference and `decisions.md` is a flat decision log, this file is the narrative
that ties both together — read top to bottom to understand the whole system, not just one piece of it.

---

## 1. What this is, in one paragraph

A Python/Playwright/LangGraph tool that crawls a WooCommerce, Shopify, or generic WordPress store
and produces a Google Merchant Center (GMC) policy-compliance report: what's confirmed broken,
what's at risk, what couldn't be verified and *why* (never a bare "unknown"), each with a real GMC
policy citation, a severity/impact tier, and — where the evidence supports it — an annotated
screenshot. Scoped specifically to GMC Shopping-ads/free-listings compliance, not a general SEO or
accessibility scanner. Built incrementally, round by round, almost always driven by a real gap
found on a real store rather than by speculation — the one explicit exception (soft-404 detection,
§8 below) is called out as such.

## 2. The three doors in

Every real way work enters this system funnels into the same core pipeline (§3):

- **The CLI** (`audit.py`) — `python audit.py --url <site>`. Runs one audit synchronously, writes
  `.md`/`.docx`(on request)/`.csv` report files to disk, exits. No persistence beyond the files.
- **The API** (`app/api/main.py`, FastAPI) — `POST /api/audits` kicks off a background job
  (`asyncio.create_task`) and returns a `job_id` immediately; the frontend polls
  `GET /api/audits/{job_id}` for phase-by-phase progress, then downloads
  `report.{md,docx,pdf,csv}` once done. A shared Playwright browser and LLM cache are kept alive
  for the whole process's life, not relaunched per request.
- **The scheduler** (`app/monitor_service.py` + `app/scheduling.py`) — a registered store gets a
  full re-audit on its own interval, on a cheap-check-detected content change, or (see §9) when a
  tracked GMC policy page itself changes. Persists every run as an `AuditRun` row, including the
  delta against the store's own previous run.

All three eventually call `run_audit()` / `run_audit_streaming()` in `app/graph.py` — same graph,
same five nodes, same order; the "streaming" variant just reports progress after each node so a
caller (the API) can show it.

## 3. The core pipeline — five nodes, one direction, no going back

```
detect_platform → crawl_and_classify → deterministic_checks → llm_grading → compile_report
```

Built on LangGraph's `StateGraph` purely for flow control (no LangChain) — a shared `AuditState`
dict threads through all five nodes, each one reading what it needs and adding to it.

**`detect_platform`** — cheap `httpx` probes decide WooCommerce (`/wp-json/wc/v3/*`), Shopify
(`/products.json`), or generic WordPress/unknown. This decides which product-data path (§5) gets
used later, and nothing else.

**`crawl_and_classify`** — the crawl itself (`app/site_mapper.py::map_site`). This is where most of
this project's real engineering has happened, so it gets its own section (§4).

**`deterministic_checks`** — every rule-based, non-LLM check runs here: required policy pages,
business-identity consistency, contact forms, product images, duplicate listings,
external/mixed-content links, and platform-specific product-data cross-checks (§5). All of this
runs *before* any LLM call — a deliberate ordering (confirmed explicitly in §8) that means the LLM
layer never gets to decide whether a page exists; it only ever grades the substance of pages the
deterministic layer has already established are real.

**`llm_grading`** — four LLM-graded checks (§6) plus an image-vision check, each anchored to a
forced-schema verbatim quote so a verdict is never just a bare "yes/no." Runs concurrently, bounded
by a semaphore.

**`compile_report`** — attempts an annotated screenshot for every eligible finding (§7), then
`app/report.py::generate_markdown_report()` turns the (now aggregated, §10) finding list into the
actual report.

## 4. The crawl itself — where most of the hard problems live

A flat page-count cap was the very first version of this. Almost everything since has been about
making the crawl *fair* and *right-sized* rather than just fast:

- **Adaptive budget** — before crawling starts, `_adaptive_page_budget()` sizes the real page
  cap from the best available signal (WooCommerce's own product count via a cheap `X-WP-Total`
  probe, else the sitemap's catalog-tagged URL count, else its total count, else a flat default),
  scaled by a formula with a floor and a hard ceiling. A flat 150-page default was either wasteful
  (a 20-product store) or far too small (a 5,000-product catalog) — this fixes both, and only
  applies when the caller didn't pass an explicit override.
- **Per-category caps, and why they weren't enough alone** — `crawl_max_product_pages_per_category`
  stops one category from swallowing the whole page budget. Live testing on a real ~111-category
  store (`britanniagifts.us`) found this *alone* wasn't sufficient: the BFS wave-building loop
  appended each source page's children sequentially before moving to the next page in a batch, and
  the following iteration's truncation always cut from the end - so whichever collection page
  happened to be processed *first* in a batch dominated the surviving wave regardless of the caps.
  Fixed with round-robin interleaving (`itertools.zip_longest`) across a batch's source pages -
  proven with a regression test that reproduces the exact shape and fails without the fix.
- **Politeness and resilience** — per-domain rate throttling, HTTP-status-aware retry/backoff, and
  anti-bot JS-interstitial detection with an actual resolution wait (promoted from a hardcoded
  6-second constant to a tunable setting after a real store's challenge took ~7-10s to clear).
- **Never a confident negative from an incomplete crawl** — if the crawl gets nothing at all (or
  robots.txt refuses it outright), the whole audit reports "could not run," never five separate
  confident "missing page" findings derived from zero information. Every unreachable page carries
  one of a fixed set of honest failure categories (`not_found`, `blocked_ssrf`, `captcha_blocked`,
  `bot_blocked`, `rate_limited`, `network_error`, `http_error`, `unknown`, and — as of this round —
  `likely_soft_404`), each with its own recommendation, never a generic shrug.
- **The soft-404 baseline probe** (this round, §8) — one extra fetch after the real crawl finishes:
  a guaranteed-nonexistent URL, using the exact same fetcher every real page went through, to
  establish "what does this site's generic fallback content actually look like."

## 5. Deterministic checks — everything that needs no LLM

Pure logic over the already-built site map, plus a couple of real network probes (image/form
reachability) that were deliberately excluded from the crawl itself:

- **Required policy pages** (privacy, shipping, returns, terms, contact) — present/missing, with a
  fallback to "could not confirm" when the site's content language isn't one the classifier
  recognizes, rather than a false "missing" verdict. As of this round, also downgrades to
  "could not confirm" when a technically-reachable candidate's content looks like a generic
  catch-all page rather than the real thing (§8) — never independently concludes "missing" from
  that signal alone.
- **Business-identity consistency** — the same email/phone/address extracted everywhere it appears
  across the site, flagged if two genuinely different values show up (with a real calling-code-vs-
  stated-country mismatch check on top).
- **Contact forms** — completeness and action-URL reachability (with the same honest failure
  categories as everything else — a DNS hiccup on a form's action URL was once mis-reported as a
  confirmed SSRF block; fixed by classifying it correctly ahead of that check).
- **Product images and product data** — platform-aware: WooCommerce/Shopify APIs are used when
  available and credentialed (cross-checked against the crawled page for consistency), generic
  page-scraping otherwise. Missing alt text, broken images, low resolution, placeholder filenames.
- **Duplicate product listings** — exact and near-duplicate (0.92 `difflib.SequenceMatcher`
  threshold, chosen once and reused everywhere this project needs a near-duplicate judgment,
  including the new soft-404 check, §8) title matching across different URLs.
- **HTTPS/mixed-content, external-domain links, duplicated nav/footer templates.**

## 6. LLM-graded checks — never a bare verdict

Four checks (`app/llm/checks.py`), Claude or OpenAI depending on configuration:
`check_policy_page_substance` (does a policy page actually say enough), `check_editorial_quality`,
`check_prohibited_content` (counterfeit/brand-risk screening, careful never to flag a brand name
alone), and `check_claim_policy_contradiction` (a marketing claim vs. the store's own stated
policy).

Every one of these is built around the same anti-hallucination pattern: a forced tool-use schema
requiring a verbatim quote from the actual page, never a bare "yes/no" the reader has to take on
faith. Two rounds hardened this further after live testing found real gaps:

- A deterministic backstop was added on top of the prompt for claim-contradiction checking, because
  prompt wording alone didn't stop the model from treating two claims with the *identical* day
  count as a genuine "different timeframe" conflict.
- Evidence-quote *fidelity* itself was verified (`verify_evidence_quote`) after a real run put
  analytical prose into a field the schema requires to be a literal quote. Verification runs
  entirely in-memory against the exact page text the model was actually shown - no live page visit
  needed. A failed check downgrades confidence, never discards the finding - this project's pattern
  everywhere: disclose and downgrade, never silently drop.

Sampling for editorial/prohibited-content checks scales with catalog size and is risk-weighted
(price outliers within the store's own catalog, thin product copy) rather than a flat first-5,
with the real coverage fraction always disclosed in the report rather than silently partial.

## 7. Annotated screenshots — proof, not just a claim

For a Suspension Risk finding whose evidence quote is real (verified per §6), a second lightweight
page visit locates that exact quote live in the rendered DOM (never a model-invented selector) and
captures a cropped, highlighted screenshot. Two real bugs were found live building this: a clip-
height clamp that was silently a no-op (`min(x, x)`, crashing the capture on a tall element), and
`scrollIntoView()` not applying synchronously on a page with `scroll-behavior: smooth` CSS (fixed
with an explicit `behavior: "instant"` override). Scoped deliberately to Suspension Risk findings
only - a site-wide aggregate finding has no single element to anchor to.

## 8. Soft-404/catch-all detection — this round, and the one genuinely hypothesis-driven addition

Every fix up to this point in the project was driven by a real bad report on a real store. This one
started differently: an external architectural review raised a *generic* concern (HTTP 200 doesn't
guarantee genuinely distinct content — a soft-404 template or an SPA catch-all route can return 200
for any URL) with no specific live-observed case behind it yet, and proposed a full new class
hierarchy to address it. Built instead as one small, targeted addition to the existing framework:

- **Once per audit**, after the real crawl finishes, one extra fetch to a guaranteed-nonexistent URL
  (an audit-scoped UUID token, not a fixed path reused across runs) — using the exact same fetcher
  every real candidate page went through, so the comparison is apples-to-apples even against a
  JS-rendered SPA fallback.
- **Two tiers of "strong match," both reused, neither invented**: an exact content-hash match
  (`app/change_detection.py`, already built for the cheap change-detection check), or a
  near-identical normalized-text match at the *same* 0.92 `SequenceMatcher` threshold
  `duplicate_products.py` already uses and has already been exercised against real stores. A third,
  weaker tier the brief described ("same title + H1 + highly similar body") was deliberately *not*
  built as its own trigger — it would need a second, uncalibrated threshold, exactly the kind of
  fabricated-precision mistake this project corrected once already.
- **The core invariant, enforced in code, not just in the docstring**: a strong match only ever
  *downgrades* a would-be "confirmed present" candidate to CANNOT_VERIFY. It can never, by itself,
  produce a CONFIRMED "missing page" finding — ambiguous evidence always means "couldn't verify,"
  never "confirmed absent."
- **Confirmed, not assumed**, that the LLM layer has no part in this decision at all: node order
  already ran deterministic checks before LLM grading, and the LLM policy-substance check already
  only ever ran on pages the crawl had already confirmed reachable.
- **This one got a real live confirmation almost immediately anyway.** A routine smoke-test crawl of
  `leafloop.site` (not a deliberate hunt for this bug) hit a genuine instance on the first try: the
  audit's own nonexistent-URL probe got redirected to a real `/lander` catch-all page, and separately
  a real crawled URL classified as that store's privacy policy had *also* been redirected there — it
  turned out `leafloop.site`'s domain had genuinely expired and is now a GoDaddy parked-domain page,
  not a hypothetical. The new check correctly caught it — downgrading to CANNOT_VERIFY instead of
  silently treating the catch-all page as genuinely present.
- **That same live run surfaced a second, related bug: two checks disagreeing about one page.** The
  soft-404-flagged page still got independently graded by `check_policy_page_substance`, which
  produced its own, redundant "lacks required substance" finding alongside the new CANNOT_VERIFY one
  — the exact same bug shape already fixed twice elsewhere in this project (two checks quietly
  disagreeing about the same underlying fact). **Fixed** with a single shared function
  (`app.soft_404_detection.soft_404_flagged_page_urls`) both `check_required_pages` and
  `run_llm_checks` now consult — computed fresh each call, never cached, the same reasoning
  `SiteMap.crawl_totally_failed` already uses as a property rather than a stored flag. A
  soft-404-flagged page is now skipped entirely for LLM substance grading and for claim-vs-policy
  contradiction checking (where it would otherwise be the *comparison target*), not
  graded-with-a-caveat — the deterministic layer's own finding already covers it fully. Re-confirmed
  live on the same `leafloop.site` page: the redundant finding is gone; the one remaining LLM
  finding is a legitimate, unrelated editorial-quality check on the homepage itself
  ("leafloop.site has expired and is parked..."), correctly left untouched since it isn't a
  required-page-candidate grading at all.

## 9. Keeping stores under watch — history, deltas, and policy-triggered re-audits

A monitored store gets: a cheap homepage-only content/DOM-hash check on its own short interval
(only escalating to a full re-audit if something actually changed), a full audit on its own longer
interval, and — independently of both — a full re-audit the moment a tracked GMC policy source page
itself changes (`app/policy_watcher.py`, checked against real, live-scraped GMC Help Center text,
not a static snapshot). Every retained run is now browsable with its own delta against its
predecessor (previously computed but silently discarded before being persisted). Deliberately: a
policy-change re-audit fires for *every* opted-in store, not just ones with past findings in that
area, since a newly added requirement can affect a store that was previously clean there.

## 10. The report itself — and why it got smaller

Markdown is the source of truth; `.docx`/`.pdf` reuse the same limited parser (not general Markdown
parsers - exactly the subset this project's own generator produces). The originally-reported problem
(a different, uncapped crawl of a large real catalog, `britanniagifts.us`) came out to 3,327 pages
and 6,337 findings - nearly all of it a handful of repeated patterns (hardcoded links, missing alt
text, broken images) logged as one fully-detailed finding *per instance* instead of aggregated the
way the business-identity check already aggregated a phone-number inconsistency into one finding
with a page list. Generalized that existing pattern (`app/finding_aggregation.py`) rather than
inventing something new - three different real shapes needed three different aggregation keys
(exact link value, link *domain* once social-share buttons were found to embed a different query
string on every page, or the whole check_id when the same problem recurs across many genuinely
distinct items). A parallel CSV export (`app/report_csv.py`) always contains every raw,
unaggregated instance, so aggregating the human-readable report never costs anyone who needs the
full detail.

**Real numbers, from a fresh live re-run of `britanniagifts.us`** (150 pages, this project's own
adaptive page budget, not an artificially large crawl): **474 raw findings**, aggregated down to
**24** in the human-readable report (**440 Markdown lines**, **21 PDF pages**). The CSV export
contains **474 data rows** - matching the raw finding count exactly, confirming aggregation
re-presents the data rather than losing any of it.

## 11. What's genuinely left

Every specific engineering item raised across every round to date has been built, tested, and
validated against real data from real stores - most recently, the soft-404/LLM-substance overlap
(§8) and the real aggregation numbers (§10). Two honest things stated plainly rather than glossed
over, not resolved by anything in this file:

- **Several of these fixes currently rest on exactly one real confirming example each** - the
  soft-404 detector (one real catch, `leafloop.site`), the annotated-screenshot mechanism (one real
  working example, also `leafloop.site`), and purchase-journey validation (one self-built
  WooCommerce sandbox, never a Shopify checkout or a second real WooCommerce store's different
  theme/plugin combination). One real success is genuine evidence something *works*; it isn't yet
  evidence it's *reliable* across the range of real stores this tool is meant to audit.
- **The accuracy validation set** - a real, human, per-category ground-truth pass over 5 real
  stores - remains the one item that has to be the user's own manual judgment call, not something
  this tool can generate for itself without becoming its own judge.

Also worth naming, not resolving: any claim of overall reliability here is a snapshot, not a
permanent state - the RAG index depends on Google's policy pages staying as scraped, and a new
failure mode can always show up on a store shape nobody's tried yet. The freshness watcher (§9)
has to keep actually catching real changes over time to keep that snapshot current.

---

See `decisions.md` for the full reasoning behind every choice named above, and `execution_flow.md`
for the precise, currently-accurate call graph. This file gets updated alongside both whenever a
new round changes the shape of the journey described here.
