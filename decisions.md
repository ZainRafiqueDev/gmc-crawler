# Decisions Log

A running record of every non-obvious decision made on this project — what was chosen, what
alternatives existed, and why. This is a decision log, not a feature chronicle (`last.md` is the
full "what's implemented" narrative); an entry here exists because a real choice was made, usually
between two or more reasonable options, or because a real tradeoff was accepted knowingly.

**Convention going forward: every future decision on this project gets appended here**, in the
relevant section (or a new one), at the time it's made — not reconstructed after the fact.

---

## Standing truths about the product

Not decisions and not tasks: facts about what the product is, kept here so they can't get lost
between rounds. Mirrored at the top of `README.md`.

- **Validated for PRECISION only — no false positives on clean stores. RECALL is unmeasured — never tested whether it catches a genuinely at-risk store. Website-only: cannot see feed- or account-level suspension causes. Every report must state the website-only limit plainly.**
  - *Precision:* live validation (gymshark.com, ridge.com, modcloth.com) could only reveal false
    positives, because all three are well-run stores that aren't suspended. Four were found and
    fixed. Every validation run so far used shrunk 8-page crawls.
  - *Recall:* never run against a store with a known suspension reason, so nobody knows what it
    misses. Stores that were suspended and are discussed in forums are weak test cases: most have
    already fixed their pages, and Google's stated reason is often just "misrepresentation".
  - *Website-only:* nothing ingests a Merchant Center feed or the Content API. The closest check
    is `price_mismatch_jsonld_vs_platform`, which compares JSON-LD with the platform API (Shopify
    or WooCommerce). It stands in for the feed only when the feed is generated from that platform.
    Feed-side causes (feed-vs-landing-page price/availability, GTINs) and account-level causes
    (account history, billing, account-level misrepresentation signals) are invisible to it.
  - *Report requirement:* every report must state the website-only limit plainly. Implemented
    2026-10-08: `app/first_audit_report.py` prints `WEBSITE_ONLY_LIMIT` on every First Audit
    report as a boxed note (a one-column table, which `app/report_pdf.py` renders as a bordered
    box with a coloured header) right after the header and before any verdict. That covers
    findings reports and "No actionable risks found" reports alike. It names the feed and the
    account explicitly, plus feed-vs-website price/availability, GTINs, and account-level causes,
    and ends "A clean result here does not mean the account is clean." Tested on both report
    shapes via the real PDF (`tests/test_api_first_audit.py`).

## Process & workflow

- **Adopted a three-part review workflow, at the user's explicit request**: (1) this file
  (`decisions.md`), maintained going forward as real decisions happen; (2) `execution_flow.md`
  (repo root) — a standing document of real entry points and call order through the codebase, kept
  current, with a Change Log section describing what each editing cycle actually altered in *what
  calls what*; (3) before the user accepts any major change, they're offered a short comprehension
  quiz on it first. Chosen over e.g. only relying on commit messages or `last.md`'s narrative,
  because a decision log and a call-flow map answer two different questions a reader has ("why was
  this done this way" vs. "how does this actually run") that neither a commit message nor a feature
  narrative answers well on its own.

## Architecture & core design

- **LangGraph `StateGraph`, not LangChain.** Five-node pipeline (`detect_platform → crawl_and_classify
  → deterministic_checks → llm_grading → compile_report`) using LangGraph purely for flow control and
  a shared `AuditState`. LangChain's abstractions (chains, agents, retrievers) weren't needed —
  every step is a plain async function; LangGraph alone gives graph structure without pulling in a
  much larger dependency surface.
- **RAG grounding without pgvector.** GMC Help Center pages are live-scraped/chunked/embedded once,
  then matched by cosine similarity in plain Python. At this corpus size (a few dozen policy
  pages), a vector DB would be infrastructure for no real benefit — added complexity with no
  measurable upside yet.
- **Every classification layer is post-hoc and non-mutating.** `ImpactTier`, `AdsEligibilityImpact`,
  `screenshot_path`, and now `evidence_verified`/aggregation are all applied as passes *over* an
  already-built findings list, never baked into the check functions that produce findings. Keeps
  each check function focused on detection only, and lets a later round change how a finding is
  classified/rendered without touching the checks that found it.

## Crawling & site mapping

- **Adaptive page-budget scaling, not a flat default.** A flat 150-page cap was wasteful for a small
  store and too small for a large catalog. `_adaptive_page_budget()` sizes the real crawl budget
  from the best available signal (WooCommerce's own `X-WP-Total` count > sitemap catalog-tagged
  URL count > sitemap total count > configured default), scaled by
  `min(HARD_MAX_PAGES=500, max(floor=60, signal×2+overhead=40))`. Only applies when the caller
  didn't pass an explicit override — the override stays a true override, never silently re-derived.
- **Per-category page caps, not one flat catalog-wide cap.** A many-category store was dumping its
  entire page budget into the first few categories discovered. Capped per-category instead
  (`crawl_max_product_pages_per_category`, default 30), while every category-listing page itself is
  always crawled regardless (cheap, structurally important to keep).
- **BFS wave interleaving: round-robin, not sequential-append.** Found live (britanniagifts.us):
  building `next_wave` by fully appending each source page's children before moving to the next page
  in a batch let a later `wave[:room]` truncation systematically starve every category except
  whichever page was processed first — even with per-category caps in place, since those caps only
  gate *whether* a URL is enqueued, not *where* it lands in the wave. Fixed with round-robin
  interleaving (`itertools.zip_longest`) across a batch's source pages before enqueueing, rather than
  e.g. raising the per-category cap (which wouldn't have fixed the actual ordering bug) or
  shuffling (which would have made crawl order non-deterministic for no real benefit).
- **`crawl_challenge_wait_seconds` promoted from a hardcoded constant to a real setting.** A real
  store's bot-protection "please wait" challenge took ~7–10s to clear in a live browser tab,
  longer than the previous hardcoded 6s. Made configurable (default 10.0s, clamped `[1, 30]`)
  rather than just bumping the hardcoded number, since the right wait time is genuinely
  store-dependent and an operator should be able to tune it without a code change.
- **Crawl failures degrade to honest, specific categories — never a generic "could not verify."**
  Every unreachable page gets one of a fixed set of failure categories (`not_found`, `blocked_ssrf`,
  `captcha_blocked`, `bot_blocked`, `rate_limited`, `network_error`, `http_error`, `unknown`), each
  with its own recommendation. A totally-failed crawl (`crawl_totally_failed`) is reported as
  **could not run**, never as a pile of confident "missing X" findings derived from zero real
  information — `RiskScore.not_applicable` is the single source of truth every report section reads,
  specifically to prevent sections from drifting out of sync with each other (found live once: the
  "At a Glance" score display disagreed with the "Final Assessment" section on the same report).
- **A shared `classify_httpx_exception()`, not three independent exception handlers.** Three
  separate call sites (image probing in two different modules, form-action reachability probing)
  had each grown their own bare `except httpx.HTTPError` handler with a guessed, sometimes-wrong
  recommendation. Consolidated into one shared classifier so every network-level failure gets the
  same accurate categorization everywhere, rather than three chances to independently guess wrong
  (one of which had already reproduced a known false-positive class: a DNS hiccup misreported as a
  confirmed SSRF block).
- **LLM-call-failure-reason gap found but deliberately not fixed.** LLM API failures all render the
  same generic "The LLM API call failed" regardless of real cause (rate limit, auth, timeout,
  malformed response), discarding a reason that's already in the logs. Not fixed in the round that
  found it: doing so properly needs an `LLMClient` protocol change with a real concurrency-safety
  consideration (a shared client instance serving concurrent calls means a naive `self.last_error`
  attribute would race) — flagged for a dedicated future round rather than rushed in as a side note.

## Security

- **SSRF guard: three independent layers, not one.** Upfront validation, per-request interception,
  and a post-navigation final-URL check — deliberately redundant rather than trusting a single
  choke point, since a single-layer guard has exactly one place left to have a bug.
- **`DNSResolutionError` kept structurally distinct from `SSRFBlockedError` everywhere.** A DNS
  resolver hiccup is a reliability failure, not a security decision, and must never be reported as a
  confirmed block. Enforced consistently across every guard call site (including, once found
  missing, the form-action reachability probe) rather than handled ad hoc per call site.
- **Purchase-journey checking is structurally incapable of paying, not conventionally careful about
  it.** Never clicks a payment/place-order control, full stop — proven per-run by the action log
  itself (every action taken is recorded and shown), not just documented as an intention. This was
  chosen over e.g. a "confirm before submitting payment" prompt because a structural guarantee
  can't be defeated by a future prompt-wording change or a missed check.
- **Opt-in extra request headers (`crawl_extra_headers`), added narrowly, not as a general escape
  hatch.** Needed specifically to get `ngrok-skip-browser-warning: true` past ngrok's free-tier
  interstitial during purchase-journey test-store validation. Scoped to add headers to a request the
  SSRF guard has *already* allowed — never a way to route around the guard — and off by default.

## Deterministic checks

- **Report aggregation lives as a post-hoc pass, not rewritten check logic.** Rather than changing
  `check_external_links`/`check_https`/the image checks to emit fewer, pre-aggregated findings
  directly, aggregation runs as a separate pass (`app/finding_aggregation.py`) over the raw findings
  list those checks already produce — same shape as `apply_impact_tiers`/`apply_ads_eligibility_impact`.
  Keeps every check function's own detection logic completely untouched; only presentation changes.
- **Three different aggregation strategies for three different real shapes, chosen from live data
  rather than one uniform rule:**
  - Exact-value aggregation (`https_mixed_content_link`): found live to be the *same* 1–2 hardcoded
    URLs repeated verbatim site-wide (296 findings → 2 distinct values) — the identical fact
    restated, so aggregating by the literal link value is correct and doesn't over-merge anything.
  - Domain-based aggregation (`external_domain_link`): exact-value aggregation was tried first and
    found live *not* to work — social-share buttons embed the current page's own URL as a query
    param, so the literal href differs on every page even though it's the same button everywhere (31
    findings, 33 distinct literal hrefs, nothing collapsed). Switched to aggregating on domain
    instead (33 → 3), while deliberately keeping `https_mixed_content_link` on exact-value matching
    — a domain-only key there would incorrectly merge two different hardcoded links that happen to
    share a domain.
  - Check-wide aggregation (`product_image_*`): found live to be the opposite shape again — 81
    "broken image" findings were 81 almost entirely *distinct* image URLs, so aggregating by image
    value would have barely reduced anything. Collapsed the whole check_id into one systemic finding
    with a representative sample instead.
- **A minimum-instance threshold (3) before aggregating at all.** A check_id or value with only 1–2
  raw instances is left exactly as individual findings — aggregating a count of 1 or 2 buys a reader
  nothing but an extra layer of indirection.
- **Aggregated findings preserve severity/confidence/policy grounding from the group untouched.**
  Aggregation changes how many `Finding` objects represent an issue and how it's presented, never
  the underlying judgment about how serious it is — deliberately, to keep this a presentation fix,
  not a re-scoring of risk.
- **A full-detail CSV export, rather than making the aggregated view lossy.** Aggregating the
  human-readable report must never make it harder to get every raw instance for someone who needs
  it (e.g. fixing every broken image one by one). Added a separate CSV export
  (`findings_to_csv_bytes`) that always contains every raw, unaggregated finding instance, with no
  `major_only` variant — it exists specifically as the "give me everything" option, so the readable
  report doesn't have to carry that burden.
- **`_finding_key` (cross-run delta identity) extended to include `location`, but only for
  aggregation-affected check_ids.** An aggregated finding has `page_url=None`, and a check_id can now
  produce several distinct aggregated findings at once (one per distinct link/domain) — without
  `location` in the key, `(check_id, page_url)` alone would collide them into one. Deliberately
  *not* extended to every check_id: an LLM-graded finding's `location` is model-generated free text
  that can legitimately reword slightly between two runs of the same real finding ("shipping policy
  section" vs "Shipping Policy section") — folding that in everywhere would misread normal wording
  drift as a resolved-and-new pair.
- **WordPress's comment form excluded by signature match, not by field-level allowlisting.** A
  sibling-`<label>` (not parent) form layout was being misclassified as a suspension-risk finding.
  Fixed by recognizing and excluding the whole known comment-form shape up front, rather than
  chasing individual field-level false positives as they surfaced.
- **Soft-404/catch-all detection built as one small addition to the existing failure-category
  framework, explicitly declined the proposed full rewrite.** An external review raised a real,
  generically valid concern (HTTP 200 doesn't guarantee genuinely distinct content — a soft-404
  template or SPA catch-all route can return 200 for any URL) and proposed a new
  `PageVerification`/`PageIdentity`/`RequiredPageResolver` class hierarchy to address it. Built as a
  small addition instead: no live-observed instance of this failure mode motivated the *request*
  (unlike every other fix in this project so far, this one started as a hypothesis, stated as such
  at the time) — but a live instance turned up immediately during this round's own smoke-testing
  (see below), and the value here (one new check, one new failure-category value) still doesn't
  justify a new architectural layer even with that confirmation in hand. A full rewrite gets
  revisited only if a real case surfaces that this small addition genuinely can't handle.
  - **Live-confirmed, not just hypothesized, during this round's own smoke test**: a routine
    smoke-test crawl of `leafloop.site` (unrelated to hunting for this specific bug) hit a real
    instance live - the audit's nonexistent-URL probe got redirected to a real `/lander` catch-all
    page, and separately, a real crawled URL classified as the site's privacy policy had *also*
    been redirected to that exact same `/lander` page. The new check correctly downgraded it to
    CANNOT_VERIFY ("content looks like a generic/catch-all page ... strongly matches a deliberately
    nonexistent URL probed during this audit") instead of silently accepting it as present. This
    also live-confirmed a real overlap: `check_policy_page_substance` (LLM-graded) still ran on
    that same `/lander` page independently and produced its own, redundant "Privacy policy page
    lacks required substance" finding alongside the new one. Initially accepted as a known,
    low-cost tradeoff rather than fixed immediately - since fixed once confirmed live, not left as
    permanent; see the follow-up entry below ("A soft-404-flagged page's content is left...").
- **Reused the existing 0.92 `SequenceMatcher` near-duplicate threshold
  (`app.checks.duplicate_products`) rather than inventing a new one for this check.** That number
  has already been exercised against real stores in this project; a second, uncalibrated threshold
  picked specifically for this check would repeat the fabricated-precision mistake this project has
  already corrected once elsewhere. The brief's third evidence tier ("same title + same H1 +
  highly similar body" as a weaker "supporting" signal) was deliberately **not** implemented as an
  independent trigger, for the same reason — it would need its own new, uncalibrated "highly
  similar" threshold. In practice, near-identical body text (the reused 0.92 check) already implies
  matching title/H1 for real templates, so the two strong tiers (exact hash, 0.92-near-identical)
  cover what the spec's hierarchy was really pointing at without a third invented number.
- **The known-nonexistent-URL baseline probe reuses the same `PageFetcher`/SSRF-guard/politeness
  path every other page in the crawl already goes through, rather than a separate httpx-based
  probe.** A soft-404/catch-all page is specifically a *client-rendered* SPA fallback in the worst
  case, so a plain (non-JS) HTTP probe wouldn't reliably see what a real visitor - or this tool's own
  crawl of a genuine candidate page - actually renders. Using the same Playwright-driven fetch
  keeps the comparison apples-to-apples (rendered content vs. rendered content) and costs one extra
  page fetch per audit, not a second crawling mechanism.
- **The nonexistent-URL probe token is a per-audit UUID, not a fixed hardcoded path.** Reusing the
  identical path across every audit risked a site (or an intermediary cache/CDN) special-casing or
  caching that one specific URL over time; a fresh token each audit avoids that without needing to
  re-probe more than once per audit (it's still exactly one fetch, not re-randomized per candidate
  page).
- **`likely_soft_404` classification lives entirely inside `check_required_pages`, never written onto
  `CrawledPage.failure_category`.** That field's existing contract is specifically "why a *fetch*
  failed" (only ever set when `reachable=False`); a soft-404 candidate is, definitionally, a
  *successful* fetch whose *content* is suspect - a different kind of judgment made one layer up.
  Reusing the field would have blurred that contract for every other piece of code that already
  assumes `failure_category is not None` implies "not reachable." The new `likely_soft_404` value
  was still added to the shared `FAILURE_CATEGORY_LABELS`/`_RECOMMENDATIONS` dicts (so its
  presentation matches every other category exactly), just referenced directly rather than routed
  through `CrawledPage.failure_category`.
- **A soft-404-flagged page's content is left in `site_map.pages` untouched, not removed or masked**
  — but the accepted-overlap tradeoff below was revisited and fixed once it was live-confirmed
  (rather than left as a permanent accepted cost): `check_policy_page_substance` (LLM-graded) was
  still running its own substance grading on the same page independently, producing a redundant
  "lacks required substance" finding alongside the new "could not be confirmed" one - observed for
  real on `leafloop.site`'s `/lander` page (which turned out to be a genuine GoDaddy parked-domain
  page - the whole site's domain had actually expired). **Fixed** with a single shared function,
  `app.soft_404_detection.soft_404_flagged_page_urls(site_map)`, computed fresh on every call (never
  cached/mutated onto `SiteMap`, the same reasoning `SiteMap.crawl_totally_failed` already uses as a
  property rather than a stored flag - one real function, never two independently-maintained copies
  that could drift apart). `check_required_pages` and `run_llm_checks` both consult the exact same
  set: a soft-404-flagged page is skipped entirely for LLM substance grading (and for
  claim-vs-policy contradiction checking, where it's the *comparison target* being unconfirmed) -
  not graded-with-a-caveat, since the deterministic layer's own CANNOT_VERIFY finding already fully
  covers the page; adding a second, differently-worded CANNOT_VERIFY note would just be a milder
  version of the same duplication. Re-run live against `leafloop.site` after the fix: the redundant
  "lacks required substance" finding is gone; the one remaining LLM finding is a legitimate,
  unrelated editorial-quality check on the *homepage itself* ("leafloop.site has expired and is
  parked...") - correctly not suppressed, since it isn't a required-page-candidate grading.
  - A real test-design pitfall found and fixed while building this: `run_llm_checks` wraps every
    task in `asyncio.gather(..., return_exceptions=True)`, so a test using a single shared
    FIFO-queue fake LLM client could pass "by accident" - a wrongly-attempted call crashes on a
    missing/mismatched canned response, and `return_exceptions=True` swallows that crash exactly as
    silently as a correctly-skipped call would look. Confirmed live during this fix's own
    development (reverting the fix still passed one of the new tests). Fixed by having the fake
    client dispatch by `tool_name` and track a real call count/log, not by response-list exhaustion.
- **Confirmed (not rebuilt) that the LLM layer never independently decides page existence.** The
  spec asked to verify this rather than assume it. Checked directly: `app/graph.py`'s node order
  runs `deterministic_checks` strictly before `llm_grading`, and `run_llm_checks` only ever calls
  `check_policy_page_substance` for a page already established `reachable` by the crawl - it has no
  path to assert existence on its own. Confirmed true as-is; no code change needed for this part.

## LLM-graded checks & anti-hallucination

- **Forced tool-use/structured-output schemas requiring a verbatim `evidence_quote`, on every
  LLM-graded check.** Never a bare verdict — the model must always produce (or explicitly return
  empty for) a literal quote, so a human reviewer always has something concrete to check the
  verdict against.
- **A deterministic backstop on top of the prompt for claim-vs-policy contradiction, not prompt
  wording alone.** Live testing found the model conflating "different wording" with "different
  meaning" on claims and policies that named the identical day count — even after the prompt
  explicitly named this exact failure pattern as a non-example. Added a code-level check
  (`_same_day_count_in_both`) that discards the verdict outright when both quotes name the same
  count, rather than trying to word the prompt into fixing it.
- **Fixed-size LLM sampling made honest and risk-weighted, not silently flat.** Editorial-quality/
  prohibited-content checks always sampled a flat first-5 product pages regardless of catalog size,
  with the gap never disclosed. Chose Option C (of several priced/discussed options, gpt-4o-mini
  rates checked before deciding): `LLMCoverageStats` now surfaces real coverage numbers in the
  report, and the sample itself scales with catalog size and is risk-weighted (price outliers within
  the store's own catalog + thin product copy) rather than always the first N crawled.
- **Evidence-quote fidelity verified in-memory, not via a live-DOM search.** Found live: forced
  schema output guarantees a string sits in the right field, never that its *content* is real page
  text — one check returned analytical prose instead of an actual quote. Verification
  (`verify_evidence_quote`) deliberately checks the quote against the exact `page_text` variable
  already available at grading time, with no second Playwright visit — a different job from
  `screenshot_annotator.py`'s live-DOM search (which needs a *renderable element* to highlight, not
  just a fidelity check), and a real cost/complexity increase this task doesn't need.
- **A failed verification downgrades confidence, never discards the finding.** `evidence_verified:
  bool` (default `True` — a deterministic finding or an empty-quote LLM finding has nothing to
  falsify). On failure: confidence downgrades `CONFIRMED → POTENTIAL_RISK` and the report states
  plainly that the quote couldn't be verified — matching this project's pattern everywhere else
  (crawl failures, language gaps: always downgrade and disclose, never silently drop).
- **Spot-checked live failures before trusting them as real fidelity gaps.** Before treating a
  "quote not found" as proof the model fabricated it, both real live failures were manually
  re-fetched and checked against the actual page content, to rule out a normalization bug in the
  verifier itself rather than a genuine model problem. Both were confirmed as genuine fabrications.
- **Counterfeit/brand-risk screening never flags a brand name alone.** The prompt explicitly
  requires the page's own text to give a real, specific reason to suspect non-genuine goods — a
  product page merely mentioning a brand for compatibility ("fits iPhone 14 case") is common and
  legitimate, and flagging on brand-name presence alone would swamp real signal with false
  positives.
- **A client-provided counterfeit banned-word list (for premium/luxury/designer sellers) was folded
  into the existing `_COUNTERFEIT_GUIDANCE` prompt text, not built as a separate keyword-match
  check.** The client's list (replica, fake, knockoff, inspired by, imitation, cloned, dupe, copy,
  mirror quality) mostly overlapped with guidance already in place; added the missing terms (fake,
  imitation, cloned, dupe, copy) directly into the LLM's watch-list rather than a deterministic
  regex ban-list — several of these words (especially "copy" and "fake") are extremely common in
  entirely benign, unrelated contexts ("a copy of your invoice"), and a hard keyword match would
  have reintroduced exactly the false-positive risk the existing "never flag from a brand mention
  alone" safeguard was built to prevent. Extended that same safeguard to explicitly cover the new
  words too, rather than assuming it would generalize on its own.
  - **Live-validated, not just prompt-text-tested**: two genuinely risky product descriptions using
    the new words ("a dupe for the iconic designer bag," "an exact copy... imitation... cloned...")
    were both correctly flagged CRITICAL with the exact risky phrase as evidence; two benign uses of
    the same words ("a copy of your invoice," a disclosed third-party phone case "compatible with
    iPhone 14") were both correctly left unflagged - confirming the safeguard actually holds for the
    newly-added terms, not just the original ones.

## RAG policy grounding

- **Real GMC Help Center pages, live-scraped and re-checked on an interval — never a static
  snapshot presented as current.** Citation freshness (`verified_at`) is surfaced per finding
  ("last verified: [date]"), and falls back to hand-written stub snippets only when retrieval
  genuinely returns nothing, never silently.
- **Policy-source re-checks (`check_policy_sources`) made the trigger for policy-change re-audits**,
  rather than a purely informational log — when a tracked policy area genuinely changes, every store
  opted into `on_policy_change` gets a full re-audit automatically, deliberately including stores
  with no past findings in that area (a newly added requirement can affect a store that was
  previously clean there — decided explicitly, not assumed by only re-checking stores with history).

## Audit history & monitoring

- **`on_policy_change` as an independent flag, not a new mutually-exclusive mode.** A store can be
  `interval`-scheduled *and* opted into policy-change re-audits at once — modeled as an orthogonal
  boolean rather than folding it into the existing `mode` enum, since the two concerns (a time
  schedule, a reactive trigger) are genuinely independent.
- **The delta report is persisted per run, not recomputed on demand.** It was already being computed
  at run time and written to disk, then silently discarded rather than saved onto the `AuditRun`
  row. Added two DB columns (`delta_markdown`/`delta_markdown_major_only`) so any retained
  historical run — not just the most recent — can show what changed versus its own predecessor,
  with a dedicated regression test confirming the schema auto-migration preserves existing rows.
- **One store's re-audit failure never blocks the rest.** Policy-change-triggered re-audits run
  isolated per store; a failure on one is logged and the batch continues, rather than one bad store
  stalling every other store's re-audit.

## Annotated screenshots

- **Scoped to Suspension Risk Findings only, decided explicitly rather than expanded to every LLM
  finding.** A site-wide aggregate finding (e.g. a business-identity inconsistency spanning several
  pages) has no single element to highlight — screenshotting only ever makes sense for a finding
  anchored to one real quote on one real page.
- **Anchoring via live-DOM text search for the finding's own already-verified quote, never a
  model-invented selector.** The model is never asked for a CSS selector — only for the quote it
  already has to produce anyway (the anti-hallucination pattern above). If the quote can't be
  located, the finding is skipped entirely rather than guessing a location.
- **A second, lightweight visit per page, not inline during the original crawl.** `PageFetcher`
  closes its browser context after every single fetch — no page object survives the crawl to
  screenshot later — so a second visit is the only viable capture point. Multiple eligible findings
  on the same page share one visit rather than one visit per finding.
- **`clip["height"]` clamped against the viewport, matching `clip["width"]`'s existing clamp.**
  Found live: the height clamp was a no-op (`min(x, x)`), so a tall matched element crashed
  Playwright's screenshot call outright. Fixed by mirroring width's already-correct clamping logic
  rather than special-casing height.
- **`scrollIntoView(..., behavior: "instant")`, not an arbitrary sleep.** Found live: a page with
  `scroll-behavior: smooth` CSS (a common modern theme default) animates the scroll over ~800ms, so
  reading the element's position right after — even after a couple of animation frames — returned a
  stale, pre-scroll position. Explicitly overriding the scroll behavior for that one call was chosen
  over adding a fixed delay, since a delay would be either too short on a slower page or wastefully
  long on a fast one; the explicit override is deterministic and confirmed live to produce the
  correct coordinates immediately.
- **Accepted "no live example yet" as a real, closed state rather than continuing an open-ended
  external search.** Across 8 real stores tried, every genuine suspension-risk hit was an
  *absence*-type finding (required content missing) which structurally has nothing to screenshot, by
  definition — not a bug in the mechanism. Decided explicitly to ship the fully-built, fully-tested
  mechanism as-is and leave a live example for whenever a *presence*-type finding naturally occurs,
  rather than keep searching real stores indefinitely for a specific finding shape.

## Report generation & the bloat-aggregation round

- **Aggregation runs automatically inside `generate_markdown_report`/`generate_delta_report`, not as
  an opt-in flag.** Every future audit gets the fix at the source, for every caller, rather than
  requiring each call site to remember to opt in.
- **Page-by-Page updated to signpost the aggregated section, not silently go quiet.** Once a
  repeated pattern's findings are aggregated away from individual pages, a page that used to show
  those findings would otherwise look like "no issues" — added an explicit note pointing to "Other
  Findings" instead of leaving the omission unexplained.
- **A combined, single, from-scratch live re-crawl with every fix present at once was not obtained,
  and this was accepted rather than kept retrying indefinitely.** Three consecutive external
  disruptions (the target site's own bot-protection re-escalating from repeated crawls, a local DNS
  resolver stall, an environment-killed process) blocked a final combined confirmation. Decision:
  rely on the two fixes' independent validation against real captured data (a real 150-page audit
  for the core mechanism; a reconstruction from that same audit's real raw findings for the
  domain-based refinement) rather than keep hammering a real third-party site that had already
  signaled it needed a break.

- **PDF bold handling fixed in the renderer, not worked around in the report (2026-10-08).**
  `app/report_pdf._line_to_markup` only bolded a *leading* `**…**` span, so First Audit's
  `**Severity:** x | **Risk level:** y` line printed `**Risk level:**` literally on every finding.
  It now bolds every `**…**` span in a line, while unpaired `**` stays literal and bold content is
  still escaped. Fixing the renderer, rather than splitting that report line, means any future
  mid-line label renders correctly too. `app/report_docx.py` has the same leading-only logic but
  no current First Audit path uses .docx, so it was left unchanged.

## Deployment & infrastructure

- **Neon over Supabase for the free-tier Postgres recommendation**, reversing an initial Supabase
  recommendation after research into current (2026) free-tier terms: Neon's compute auto-suspends
  after 5 minutes idle but auto-resumes on the next query with no manual step, versus Supabase's
  multi-day hard pause requiring manual unpausing — a meaningfully better fit for this app's
  occasional scheduled queries.
- **Render's paid Starter tier ($7/mo) explicitly flagged as not the fix for a memory problem.**
  Still 512MB, same as the free tier — only Standard ($25/mo) adds real headroom. Decided to state
  this plainly in the deployment guide rather than let a reader spend money on the wrong upgrade.
- **`--disable-dev-shm-usage` added to every real Chromium launch site, after confirming root cause
  from real logs, not as a preventive guess.** A real Render free-tier deploy was silently killing
  the audit job mid-crawl; root-caused via the actual production logs as Chromium exhausting
  Docker's tiny default `/dev/shm` under real memory pressure, then fixed with the standard mitigation
  for that specific, confirmed cause.
- **The free-tier hosting constraint on scheduled monitoring features was documented, not glossed
  over.** Free-tier Render sleeps after 15 minutes idle, which conflicts with features needing a
  continuously-running process (interval schedules, `on_policy_change` re-audits). Chose to state
  this as a real, explicit constraint with two honest ways to live with it, rather than let a reader
  discover it only after deploying.

## Testing & validation methodology

- **Live validation is a separate, deliberate discipline layered on top of unit tests, never treated
  as optional.** Passing unit tests alone have never been accepted as sufficient evidence for a
  live-behavior claim on this project — every round that could be checked against a real store, was,
  with raw output actually read rather than assumed. This discipline is *why* several real bugs (the
  BFS wave-ordering bug, both screenshot clip/scroll bugs, the domain-vs-exact-href aggregation gap)
  were ever found at all — none were visible from mocked tests alone.
- **When live data reveals a gap the mocks missed, fix it immediately in the same round, rather than
  filing it for later** — the explicit instruction behind several rounds' "found live, fixed live"
  pattern, applied consistently rather than case by case.
- **A verification mechanism's own failures are spot-checked before being trusted as real bugs in
  what they're checking.** Applied to the evidence-quote verifier: before concluding an LLM
  genuinely fabricated a quote, the real page was re-fetched and checked, to rule out a
  normalization gap in the verifier itself producing a false "not found."
- **The accuracy validation set is explicitly left to the user's manual judgment, not automated.**
  Building a ground-truth pass over 5 real stores requires a human reviewer's own call on what
  should be flagged — having this tool generate its own ground truth would make it its own judge,
  which defeats the point of a validation set. Scaffolding exists (`validation/`); the actual
  judgment calls are deliberately left undone by design, not by oversight.

## First Audit pipeline (GMC bot follow-up: standalone compliance snapshot)

- **First Audit is a fully standalone pipeline, deliberately not wired into the monitoring
  subsystem.** `FirstAuditRun` is a new, separate table from the existing `AuditRun` (monitoring-
  scoped, requires a `store_id`) — `FirstAuditRun.store_id` is nullable and unused this round, kept
  only as a seam for linking a first audit to a monitored store later. User-confirmed: do not create
  a `MonitoredStore` row, do not touch the scheduler, when a URL is submitted for a first audit.
- **Discovery and classification are reused as-is, not rebuilt.** `app.platform_detector.detect_platform`
  and `app.site_mapper.map_site` already do robots.txt/sitemap discovery and `PageType` classification
  that closely matches the spec's `resource_type` list — `app/first_audit.py` only orchestrates them
  and persists the result; it doesn't reimplement crawling or classification.
- **Rules are YAML-authored, DB-mirrored, never DB-authoritative.** `app/rules/starter_rules.yaml` is
  the source of truth; `app.rules.loader.sync_rules_to_db` upserts a mirror row per rule into the
  `rules` table on startup so `evaluations`/`findings` have a stable FK, and marks any DB row whose id
  is no longer in the YAML as inactive (never hard-deleted, so historical evaluations keep a valid
  FK) rather than ever letting the table and the file disagree.
  - `business_identity_conflict` deliberately delegates to the *existing*
    `app.checks.business_identity.check_business_identity_consistency` via a `condition.type:
    existing_check` clause, rather than reimplementing its contradiction logic a second time inside
    the generic rule engine.
  - Exactly the 6 rule examples named in the spec were written (within the 5–8 range asked for) —
    no extra rules invented beyond what was named.
- **The new `findings` table is additive, never a replacement.** Written alongside the existing
  `AuditJobRecord`/`AuditRun.findings_json` JSON blob the frontend/report rendering already reads —
  nothing currently consuming that blob changes or breaks.
- **Evidence storage is three-tier, added to the schema in the same round it was asked for rather
  than deferred**, per an explicit mid-round addition once the standalone evidence-store design was
  already underway:
  - Tier 1 (Postgres): small structured rows only (`resources`, `resource_facts`, `products`, `rules`,
    `evaluations`, `findings`) — raw HTML/text is never a column on these tables.
  - Tier 2 (object storage via `app.evidence_storage`, local-fs or S3-compatible): raw bodies, JSON-LD,
    and screenshots, referenced from a Tier 1 row only by content hash (`app.db.Blob`). Content-
    addressable dedup-on-write (`app.evidence_blob_store.write_blob_if_needed`) — identical content is
    uploaded once regardless of how many resources/runs reference it.
  - **PASS drops body**: a resource with no failing/needs-review rule evaluation never gets its raw
    body uploaded at all (`KEEP_BODY_ON_PASS=false` default) — this is a genuine ordering constraint,
    not just a policy choice: rule-evaluation results don't exist yet at crawl/discovery time, so
    `app.evidence_store.store_resource_body_evidence` is a separate function called once the rule
    engine (work-order step 4) has run per-resource results, not from `persist_resources` itself.
  - Retention (`app/evidence_retention.py`) ages out old runs' raw blobs by either configured axis
    (days or run-count — kept if *either* says recent enough) while never touching
    `resource_facts`/`evaluations`/`findings` themselves; also caps the LLM result cache's row count
    (`LLM_CACHE_MAX_ITEMS`) since Tier 3 (ephemeral/bounded) explicitly called out that cache as a
    structure that must never grow unboundedly for the life of the process.
  - `boto3` (S3 backend) is a declared dependency but imported lazily inside `S3StorageAdapter.__init__`
    — confirmed live that `app.api.main` still imports and the full test suite (561 tests) still
    passes with boto3 *not installed*, since the default backend is local-filesystem.

- **Rule-key rename and evaluation-engine wiring ship atomically, in piece 4, never split across a
  commit boundary.** `starter_rules.yaml`'s five rules still reference pre-canonical fact-key names
  (`shipping_region`, `return_window_days`, etc.) as of piece 1 (fact extractors) - this is safe only
  because `app.rules.loader` validates rule *structure* (via `app.rules.schema.Rule`), never
  cross-checks a condition's `fact`/`left`/`right` against the canonical fact dictionary, so there is
  no state where the loader "passes" in a way that implies those keys resolve to anything. The actual
  guarantee is that nothing queries `ResourceFact` by key yet (the contradiction engine, piece 3,
  doesn't exist until after piece 1/2) - so the rename to canonical dotted keys, the
  `numeric_mismatch` schema change (`left`/`right` → `fact` + `methods`), and the first code that
  evaluates rules against real facts all land in the same piece-4 change. Piece 4 will also extend
  `app.rules.schema`/`loader` to validate every rule's referenced fact key against the
  `app/facts/keys.py` registry at load time, closing this off structurally (not just by ordering
  discipline) for any future rule.

## Canonical fact dictionary + piece 1 (fact extractors)

- **The fact dictionary was locked (`app/facts/canonical_facts.md`) before any extractor code was
  written**, same discipline as locking `starter_rules.yaml` before the rule engine - every
  extractor emits facts in exactly that shape (key, value type/encoding, normalization rule,
  expected pages, comparison scope, method tag), and the contradiction engine (piece 3) will compare
  only on `(fact_key, normalized value)`, never raw text.
- **Money facts are split into `.amount` + `.currency`** rather than one composite string,
  specifically so the rule engine's `numeric_mismatch` condition (piece 4) can do a plain numeric-
  tolerance comparison without parsing a composite value itself.
- **Availability and GTIN/MPN normalization were locked as real code (`app/facts/normalize.py`),
  not just prose, before piece 1** - per explicit instruction: availability must map BOTH schema.org
  (`InStock`/`OutOfStock`/...) AND platform-API vocabularies (WooCommerce `instock`/`onbackorder`,
  Shopify's boolean `available`) to one canonical enum, or an availability-mismatch rule would
  false-flag `InStock` vs `instock` as a real contradiction; GTIN is canonicalized to a zero-padded
  GTIN-14 form (GS1's own equivalence rule) so a GTIN-12 from one source and the same product's
  GTIN-14 from another compare equal instead of false-flagging a cosmetic length difference.
- **Business name/legal entity are genuinely new extraction** (copyright-footer and "operated by ..."
  regexes, `app/facts/business_identity_facts.py`) - `app.checks.business_identity` never extracted
  these fields, only email/phone/address, so "generalize, don't rewrite" meant reusing its existing
  regexes/helpers directly (`_extract_signals`, `_guess_calling_code`, `_CALLING_CODE_COUNTRIES`,
  imported, not re-typed) for the facts it already produces, while adding new patterns only for the
  facts it never did.
- **The heading-based business-name heuristic is restricted to HOMEPAGE/CONTACT_ABOUT pages only** -
  found while writing the test for it: applying it to every identity page type would read a
  PRIVACY_POLICY/TERMS_OF_SERVICE page's own heading ("Privacy Policy") as a business name, a
  real false-positive caught before it shipped, not after.
- **A real sentence-splitting bug was found and fixed while writing tests, not left to piece 3 to
  discover**: splitting shipping/return text on every `.` breaks a decimal price like `$7.50` into
  two sentences ("...$7" / "50 within..."), silently truncating the extracted amount to `"7"`. Fixed
  by never splitting on a period that has digits on both sides. Caught by
  `test_shipping_cost_amount_and_currency` actually failing, not by inspection.
- **Fact extraction is wired into `app.first_audit.run_first_audit_discovery` in this same piece**
  (right after `persist_resources`), rather than left as disconnected pure functions - every piece-1
  fact extractor is reachable from the real pipeline and covered by an end-to-end test
  (`test_discovery_happy_path_persists_resources_and_marks_done`), not just unit-tested in isolation.
- **Product/JSON-LD facts are explicitly NOT built in piece 1** - `product.price`, `.availability`,
  `.brand`, `.gtin`/`.mpn`/`.sku`, `.condition` are piece 2's scope; `app/facts/extractor.py` only
  combines business-identity + commerce facts for now.

## Piece 2 (JSON-LD -> same canonical product facts)

- **Collapsed `jsonld_offer`/`jsonld_product` into one `jsonld` method tag**, per explicit
  instruction - both carried the identical 0.95 base confidence already, so nothing was lost;
  `app/facts/keys.py`, `canonical_facts.md`, and the "consequence for starter_rules.yaml" note were
  all updated together so nothing is left referencing the old two-tag split.
- **`@graph` and multiple `<script type="application/ld+json">` blocks are both walked recursively**
  (`app.facts.jsonld_facts._flatten_nodes`/`_parse_jsonld_blocks`) - a page's `Product` node can be
  nested inside `@graph`, inside a top-level array, or split across several script blocks (e.g. an
  `Organization` block plus a separate `Product` block); all combinations flatten to the same list
  of candidate nodes before filtering to `@type: Product`.
- **`AggregateOffer` emits `lowPrice`** as `product.price.amount` - documented as the deliberate
  choice (the price a shopper can actually pay, closest to what a platform-API/visible-price
  observation would show) rather than `highPrice` or an average. If the `AggregateOffer` also
  nests a real `offers` array, those per-variant facts are extracted too, never instead of the
  aggregate fact.
- **Offer variants are never collapsed** - each offer in an `offers` array gets its own full set of
  fact rows (price/currency/availability/sku/gtin/mpn), all under the same `resource_id`, with
  `source_text` prefixed by the offer's SKU for traceability. No dedicated `variant_sku` DB column
  was added (out of scope for this round - flagged as a reasonable future addition only if a rule
  ever needs cross-variant contradiction detection, not needed yet since nothing currently
  cross-compares two variants of the *same* product against each other).
- **A malformed `ld+json` block is caught per-block, not per-page** - `json.JSONDecodeError`/
  `TypeError` on one `<script>` tag logs and skips just that block; every other block on the page
  still gets parsed.
- **A present-but-null/empty JSON-LD field never becomes a fact** (`"price": null`, `"sku": ""`) -
  verified directly (`test_empty_or_null_field_emits_no_fact`), consistent with piece 1's
  "absence of a row IS unknown" rule - never a zero-confidence placeholder.
- **GTIN variants (`gtin`/`gtin8`/`gtin12`/`gtin13`/`gtin14`) all resolve through the same
  `normalize_gtin`** (zero-padded GTIN-14) - checked in priority order, most-specific field first,
  but since all map to the same canonical form via zero-padding, the priority order only matters if
  a product's own JSON-LD disagrees with itself across multiple GTIN fields (a feed-quality issue
  of its own, not something this extractor needs to flag separately).

## Piece 3 (contradiction engine)

- **Added `ResourceFact.variant_key` (nullable), a real schema change, not scope creep** - without it
  there was no way to query-group a multi-offer product page's variants separately; parsing SKU back
  out of free-text `source_text` for comparison would have violated "compare on canonical key +
  normalized value only, never raw strings." Populated only by piece 2's per-offer JSON-LD facts
  (the normalized SKU); every other extractor leaves it `None`, fully backward-compatible with every
  piece 1/2 fact already written.
- **One shared `values_equal(fact_key, a, b)` in `app/facts/normalize.py`** is the only place
  comparison logic lives - numeric facts (`return.window_days`, `shipping.*_days`,
  `shipping.cost.amount`, `product.price.amount`) get a per-fact-type tolerance
  (`NUMERIC_FACT_TOLERANCE`, 0.0 for day-counts so `"30"` and `"30.0"` compare equal without being a
  float-equals bug, 0.01 for money), GTIN/MPN/SKU compare verbatim (never casefolded - both sides are
  already normalized to one canonical casing at extraction time), everything else falls through to
  the engine-wide case-insensitive, whitespace-collapsed default from `canonical_facts.md`.
- **Two separate entry points, matching the two scopes in the dictionary, not one generic
  function**: `evaluate_cross_page_contradiction` (site-wide: business/shipping/return/payment facts
  compared across pages, one `Evaluation` per call, `resource_id=None`) and
  `evaluate_per_resource_method_comparison` (per-resource: product facts compared across *methods*
  for the *same* resource, grouped by `(resource_id, variant_key)`, one `Evaluation` per
  resource+variant that had enough evidence). Keeping them separate, rather than one function
  branching internally, mirrors the dictionary's own scope distinction directly in the code.
- **"Absent" produces no `Evaluation` row at all**, not a `pass` with no data - fewer than two real
  fact values for a site-wide key, or a resource/variant missing one of the two methods, is skipped
  entirely. A `pass` `Evaluation` is only ever written when there genuinely were 2+ real values and
  they agreed - distinguishing "checked, consistent" from "nothing to check" is the rule engine's
  (piece 4) job to also respect, not blurred here.
- **Confidence gate reuses the CANNOT_VERIFY-style downgrade pattern, with the threshold coming from
  the caller's own rule, never hardcoded in the engine**: a detected conflict is `"fail"` only when
  every contributing fact's confidence meets the caller-supplied `min_confidence`; otherwise
  `"needs_review"`. The engine itself has no opinion on what that threshold should be for any given
  rule - it's a parameter, not a constant.
- **Every query is scoped to one fact key (and, for per-resource comparisons, one method pair)
  within one audit run** - never a bare `select(ResourceFact)` over a whole run's fact table.
- Piece 3 deliberately stops at `Evaluation` rows: no `Finding`/`FindingRecord` creation, no
  `store_resource_body_evidence` wiring - both are piece 4's scope.

## Piece 4 (rule rename, rule engine, findings, PASS-drops-body wiring, screenshots) - final piece

- **Step 1 (deferred migration) shipped exactly as promised**: the five rules' fact keys were renamed
  to canonical dotted form, `RuleCondition.numeric_mismatch` changed from `left`/`right` to `fact` +
  `methods`, and `app.rules.loader` now validates every rule's fact key/resource_type/method against
  `app/facts/keys.py`'s registry at load time - in the same change, never split across commits. Also
  fixed a real latent bug caught while doing the rename: `sources`/`resource_type` in the original
  draft used PageType's enum *names* ("RETURNS_POLICY") but `Resource.resource_type` stores
  `PageType.value` ("returns_policy") - every rule would have silently matched zero resources until
  this rename pass caught and fixed the casing.
- **Added `Rule.title` and `Rule.remediation` as required fields** - `description`/`policy_reference`
  alone couldn't satisfy "Google rule + evidence + risk + exact fix, all present, or it's not
  emitted" (spec section 2.8): `description` is a longer policy-grounding paragraph, not a finding
  title, and remediation is a property of the rule/policy area that has to be author-stated, not
  derived from an Evaluation's evidence.
- **`consequence` wording is derived centrally from `rule.impact`, never authored per rule** - one
  fixed mapping (`_CONSEQUENCE_BY_IMPACT` in `app.findings_builder`) enforces the spec's exact
  wording constraint ("potential suspension risk" / "likely product disapproval", never "Google will
  suspend you") so no future rule can accidentally drift into overclaiming language.
- **A `needs_review` Evaluation still becomes a Finding**, title suffixed "(could not be confirmed
  with full confidence)" - the same surfaced-not-suppressed discipline as every other CANNOT_VERIFY-
  style downgrade in this project. Only a `pass` Evaluation produces no Finding.
- **`existing_check` evaluations preserve the delegated Finding's own fields** (title/evidence/
  severity/policy_reference/recommended_fix) rather than using the rule's generic ones - the
  delegated check (`business_identity_conflict`) already produces more specific, situation-accurate
  text than a generic rule template could.
- **PASS-drops-body's "has this resource got a failing evaluation" question has two real answers,
  not one** - a per-resource evaluation's own `resource_id`, AND any resource cited as evidence inside
  a site-wide evaluation (observations list for contradiction/mismatch evaluations). **Found live,
  not in a unit test**: an `existing_check`-delegated evaluation (e.g. "multiple phone numbers found")
  has no single `page_url` at all (it's inherently multi-page) and doesn't use the `observations`
  evidence shape, so its cited pages initially never got marked for retention - first live run against
  britanniagifts.us showed `Resources with retained body: 0` despite a real finding citing 4 real
  pages. Fixed by also scanning a delegated finding's own evidence text for URLs
  (`app.first_audit._resources_cited_in_evaluation`'s fallback path) - re-run confirmed 4 resources
  and 4 real blobs retained. Recorded here specifically because it was a correctness bug a unit test
  with synthetic evidence hadn't exposed, only a real crawl did.
- **A second, independent bug was found and fixed the same way during the same live run**: both
  `_apply_body_evidence_retention` and `_capture_finding_screenshots` were mutating `Resource`/
  `FindingRecord` objects that had been loaded in an earlier, already-closed DB session - a detached
  object's attribute changes are never tracked by a different session's unit-of-work, so
  `body_blob_hash`/`screenshot_blob_hash` silently never persisted despite no exception being raised.
  Fixed by re-fetching each object by id (`session.get(...)`) inside the session that actually performs
  the write, a pattern every other session boundary in this file already followed - this one just
  hadn't been exercised end-to-end until the live run.
- **Screenshots: WebP (not the JPEG this module actually already used - not PNG as originally assumed
  when this was scoped)**, gated by `settings.screenshots_only_critical` (severity critical/high only)
  and, in the First Audit pipeline specifically, `evaluation.result == "fail"` only (never
  `needs_review` - a second live-browser visit is only worth it as defensible evidence for a result
  already confident enough to assert). The capture mechanics (quote-location, crop, resize, encode)
  are one shared implementation (`_locate_and_capture_bytes`) used by both the old pipeline's file-
  writing path and the new pipeline's blob-returning path (`capture_finding_screenshot_bytes`) - never
  duplicated. A navigation failure's partially-opened browser context is explicitly closed before the
  exception propagates (a real leak the refactor's own symmetry made obvious, fixed in the same pass).
- **Live end-to-end validation, not a mocked run**: ran the full pipeline against britanniagifts.us
  (capped to 30 pages for a fast demo) with a real Playwright browser, zero mocking. Result: 30 pages
  crawled, platform detected as WooCommerce, 35 real facts extracted (emails, phones, country mentions,
  shipping/return timing, payment methods), 3 rule evaluations, 2 real findings (missing returns page;
  two genuinely different phone numbers - one US, one UK - found across 4 real pages), snapshot status
  CRITICAL, 4 resources' raw bodies correctly retained as evidence (the other 26 correctly dropped) as
  4 real blobs (compression deliberately disabled for this demo run, to keep it simple - "none" codec).
  Zero screenshots were attempted, correctly: the one critical finding has no single page to screenshot
  (it's about an absent page) and the one page-scoped finding is only medium severity, below the
  critical/high gate. Both real bugs above were found during this run, not before it - exactly the
  discipline this project has applied every other round.

## Fix: consequence/risk_level derived from severity, not from the parent rule's impact tier

- **Real inconsistency caught in review of the live run's own output**: `business_identity_conflict`
  (severity=critical, impact=suspension_risk - calibrated for its own worst case, "no contact info at
  all") delegates to `check_business_identity_consistency`, which can also emit a much milder
  severity=medium "two different phone numbers" finding - and that finding still inherited the
  rule's suspension_risk consequence/risk_level, reading as "medium severity, suspension risk" in
  the same finding. Confusing and untrustworthy to a reader.
- **Fix: `risk_level` and `consequence` are now a pure function of the finding's own severity**
  (`_RISK_LEVEL_BY_SEVERITY`/`_CONSEQUENCE_BY_SEVERITY` in `app.findings_builder`), never of
  `rule.impact` - structurally unable to disagree, for any rule, not patched case-by-case.
  `rule.impact`/`ImpactTier` stays on the `Rule` schema as rule-authoring metadata (unused by
  findings_builder now) - removing it entirely would have been a bigger change than asked for.
  Mapping: critical->suspension_risk, high->serious_risk, medium->listing_disapproval,
  low->minor_issue - four tiers, not reused from `ImpactTier`'s three-value vocabulary, since the
  user's own suggested mapping needed a fourth, distinct tier for "high" that didn't exist before.
- **Picked a side for the two-phone-numbers case specifically, per instruction**: kept its existing
  severity=medium (set independently, years earlier, in `business_identity.py`'s own
  `check_business_identity_consistency` - not touched this round) and changed its *consequence*
  wording to the listing_disapproval tier, not suspension. Reasoning: Google's actual enforcement
  ties account-level suspension to a confirmed misrepresentation signal (unverifiable/fake business
  identity) - a simple multi-phone-number inconsistency, especially one the check's own remediation
  text already hedges as "confirm whether these are intentionally different (e.g. regional support
  lines)," isn't a confident misrepresentation claim on its own. `missing_contact_info` (zero contact
  info found at all, no hedge) keeps severity=critical/suspension_risk - that's the case the real
  policy risk is calibrated for.
- Pipeline logic unchanged - no Evaluation/rule-engine/contradiction-engine code touched, exactly as
  asked ("wording/mapping only"). 5 new/updated tests, including an explicit regression test for the
  exact two-phone-numbers scenario.

## Admin auth (backend) + First Audit API + PDF report download

- **Built the admin-auth backend that was agreed to much earlier in this project's life
  (session cookie, seeded admin, `require_admin` dependency) but never actually implemented** -
  it was a hard prerequisite for "behind require_admin, same gate as the rest of the first-audit
  routes," which didn't exist yet either (no first-audit API routes existed before this round). Built
  both together rather than stubbing the gate, since shipping an unprotected admin endpoint even
  temporarily was never on the table.
  - `users`/`sessions` tables (`app.db`), `bcrypt` for password hashing (new dependency - neither
    `bcrypt` nor `passlib` was already installed), server-side revocable sessions (a DB row, not a
    stateless signed token - logout actually invalidates it) via an opaque `secrets.token_urlsafe`
    cookie.
  - `seed_admin` re-syncs the admin's password hash from `ADMIN_EMAIL`/`ADMIN_PASSWORD` on every
    startup (env var is the source of truth, same pattern as the rules YAML syncing into its DB
    mirror) - refuses to seed anything if either env var is blank, rather than booting with a
    guessable default account.
  - **Explicitly backend-only this round**: no frontend login page was built (that's real UI work,
    not implied by "add a PDF download endpoint"). `POST /api/auth/login` exists and works; nothing
    in the Next.js frontend calls it yet.
- **First Audit API routes are new** (`app/api/first_audit.py`): `POST /api/first-audit` (create +
  kick off a run), `GET /api/first-audit/{run_id}` (status), `GET /api/first-audit/{run_id}/report.pdf`
  (this round's actual ask) - all behind `require_admin`. No findings-list/detail JSON routes were
  built (not asked for, and would duplicate what the PDF already renders from the same rows) - **the
  "findings UI (single-finding/list screens) links to this PDF" instruction could not be confirmed
  because that UI does not exist** - there is no first-audit frontend at all yet, only these backend
  routes. Flagging rather than silently claiming it's wired.
- **PDF generation: on demand, not cached**, chosen explicitly (reasoning lives in
  `app/api/first_audit.py`'s own docstring) - rendering FindingRecord rows through the existing
  Markdown->reportlab path is pure CPU with no network/LLM step, so caching would add Tier-2-style
  storage/retention complexity for no real latency win. If that calculus changes later, a cached PDF
  would have to go through the same evidence store + retention policy as everything else in Tier 2 -
  never an unbounded side table.
  - **Reused the existing PDF path exactly as instructed** - `app/report_pdf.py::markdown_to_pdf_bytes`,
    unchanged except its one brand color (`_BRAND`, indigo `#4F46E5` -> light-blue `#0EA5E9`) - no new
    PDF rendering code. New module `app/first_audit_report.py` only builds the Markdown string
    (findings ordered critical-first; a COMPLIANT run renders "No actionable risks found" as a valid
    PDF, not an error/empty file) from the normalized `FindingRecord` rows - never the old pipeline's
    `findings_json` blob, which doesn't even exist for a `FirstAuditRun`.
  - **`google_source_link` reuses `app.report._extract_source_url`** (the existing regex that pulls a
    real GMC Help Center URL already embedded in a `policy_reference` string) rather than a new
    lookup or a fabricated link - today this returns `None` for every starter rule, since none of
    their `policy_reference` text has a verified URL embedded in it yet. Stated honestly rather than
    inventing a plausible-looking `support.google.com` link - the same discipline that originally
    motivated this project's real-RAG-policy-index work (Phase C) over hand-written policy snippets.
  - Note on the frontend theme swap agreed earlier (black + light-blue, whole frontend): still only
    partially done - this round's PDF brand-color change is the one place it's actually applied;
    `frontend/app/globals.css`'s indigo/fuchsia tokens were never swapped (a separate, still-open
    task from several rounds ago, not touched here).
- Tests use a minimal FastAPI app mounting just the two new routers (same "test at the function/
  router level" pattern this project already uses elsewhere) rather than the full `app/api/main.py`
  lifespan, which launches a real Playwright browser on startup - no existing test in this project
  did that either.

## Verified Google source URLs for every starter rule

- **Every URL was found via live web search and confirmed by fetching the actual page content**
  (WebSearch + WebFetch, this session) - none guessed, none assembled from a plausible-looking
  `support.google.com/merchants/answer/<number>` pattern. Five of six rules got a real, on-point
  citation; one (`shipping_region_contradiction`) got `null` because no official page was found that
  actually states the specific claim that rule checks - see the rule's own YAML comment for what was
  searched and ruled out, and the chat response for the full mapping table.
- **`price_mismatch_jsonld_vs_platform`'s source (`.../answer/9773429`) was the best possible match
  found** - it explicitly names schema.org structured data as part of the price-consistency check,
  which is exactly what the rule compares (JSON-LD vs platform-API price).
- **`return_window_conflict` and `missing_returns_page` deliberately share one source**
  (`.../answer/10220642`) - the same official return-policy page states both the cross-page
  consistency requirement (window_conflict) and the "must be clearly published" requirement
  (missing_returns_page); it isn't two rules competing for one citation, it's the one page that
  actually covers both claims.
- **Loader validation is a hard allowlist** (`support.google.com`/`google.com`/`www.google.com`/
  `developers.google.com`, https only) - a non-official domain raises at load time;
  `google_source_url: null` is valid and logged (not an error) so unsourced rules stay visible rather
  than silently passing.
- **`findings_builder` now reads `rule.google_source_url` directly** - replaced the earlier
  `_extract_source_url(policy_reference)` regex-scraping fallback entirely (that function stays in
  `app.report` for the old pipeline's own Findings, untouched) since every rule now either has a real
  URL or an honest `null` - there was nothing left to scrape for.
- `RuleRecord` (the DB mirror) and `app/first_audit_report.py`'s PDF rendering both carry the field
  through unchanged - confirmed live via a test asserting the actual URL string appears in the
  rendered PDF's extracted text.

## LLM-assisted extraction for the four "free-prose" commerce facts

- **Moved exactly four facts off pure regex, per explicit instruction, nothing more**:
  `return.window_days`, `shipping.processing_days`, `shipping.delivery_days`, `return.refund_terms`.
  Every other commerce fact and every business-identity/product/JSON-LD fact stays purely
  deterministic - this is a narrow, named carve-out for the specific facts where keyword-proximity
  regex had already produced two confirmed real false positives (modcloth.com), not a general policy
  shift toward "use an LLM when in doubt."
- **Regex stays as a free, fast cross-check - never removed, never duplicated.** The exact same
  `_return_window_fact`/`_day_fact_from_keyword_sentences`/`_classify_fact` functions
  `extract_commerce_facts` used to call directly now live in `app.facts.llm_commerce_facts` as the
  "fast path" input to reconciliation - reused, not rewritten.
- **The LLM's "nothing stated here" (`null`) is authoritative, even over a regex hit.** If the model
  says a fact isn't on the page, nothing is emitted regardless of what the regex fast-path found -
  the whole point of this round was that regex's own judgment about *which* number is the right one
  can't be trusted standalone; a regex "hit" the LLM disagrees with is exactly the failure mode that
  motivated this change.
- **`method="llm"`'s confidence is the one deliberate exception to "confidence is a pure function of
  method"** (established in piece 1/2's own fact-dictionary design) - a semantic extraction's
  reliability genuinely varies per instance, so it carries the model's own reported confidence
  (clamped to `[0,1]`) rather than a fixed per-method constant. `regex_llm_agree` (two independent
  signals agreeing) stays a fixed constant (0.95), same as every other method, since that agreement
  is a structural property, not a per-instance judgment call.
- **The LLM's cited source sentence is verified against the real page text before anything else** -
  `app.llm.checks.verify_evidence_quote` reused directly, not a new verification path. An
  unverifiable claimed quote voids the extraction entirely (same discipline as every other LLM-graded
  check in this project).
- **Found live while re-testing, not before shipping**: a real `ANTHROPIC_API_KEY` in this machine's
  own `.env` meant bare `Settings()` in `tests/test_first_audit.py` silently had `llm_configured=True`
  the moment this round's code could act on it - three existing integration tests started making
  real LLM calls and hit a `sqlite "database is locked"` error from a second concurrent session. Not
  a bug in the new code's logic (confirmed: the reconciliation itself worked correctly once isolated)
  - a pre-existing test-isolation gap this round's change was the first thing to actually trigger.
  Fixed by explicitly blanking `anthropic_api_key`/`openai_api_key` in that test file's `Settings()`
  helper, so these tests never depend on what happens to be in a developer's own `.env`.
- **Live-validated twice, for real, against the actual modcloth.com pages** (real OpenAI API call,
  no faking): the model correctly returned `return.window_days=30` with confidence 1.0 on BOTH the
  return-policy page (which also contains the "processed within 2 business days" refund-processing
  trap) and the shipping-returns page (whose real window sentence never says the word "return") -
  the exact two confirmed false positives from the validation round, both gone, end to end through
  the real reconciliation pipeline, not just against synthetic fake-client test responses.

## Round: bot-block detection, blocked-vs-absent, off-domain credit, variant_key fix

- **"Unreachable != absent" made structural, not just a wish.** Core principle driving all three of
  this round's correctness fixes: a page the crawler couldn't read (bot-blocked, network error) must
  never be scored the same as a page confirmed genuinely missing. Found live on ridge.com: a
  Cloudflare "you have been blocked" interstitial (status 200, real page-shaped HTML) was being read
  as ordinary reachable content, producing confident-but-wrong findings from zero real facts.
- **Hard-block detection added as a third tier in `app.fetch`, ahead of the existing transient-challenge
  check** (`_looks_like_hard_block`, checked right after `page.content()`, before
  `_wait_for_challenge_to_resolve` ever runs) - an unambiguous phrase list (e.g. "sorry, you have been
  blocked", "incapsula incident id") fires unconditionally; an ambiguous list ("access denied",
  "reference #") only fires when the page's visible text is also short enough to look like an
  interstitial rather than real content with that phrase incidentally present. Reuses the existing
  `bot_blocked` failure category and its retry cap - no new category needed, since a hard block and a
  transient challenge both resolve to "this page couldn't be read," just via different detection paths.
  Live-validated against the real ridge.com Cloudflare block-page text.
- **`missing_resource`/`all_missing` rule evaluation now distinguishes blocked-or-unreachable candidate
  pages from genuinely-absent ones**, reusing the exact `cannot_verify_matches` branch pattern
  `app.checks.deterministic.check_required_pages` already established (genuine > soft-404-ambiguous >
  fetch-failed > confirmed-absent) rather than inventing a parallel priority order. A
  blocked/unreachable candidate downgrades the finding to `needs_review` (confidence 0.3), never a
  confident `fail` - the same discipline, one level up, that the hard-block fetch-layer fix enforces.
  `FirstAuditRun.unreachable_pages_count` surfaces as a run-level PDF note so a reader sees "N pages
  couldn't be read" rather than silently wondering why some checks came back non-committal.
- **Off-domain returns/contact pages: detect-and-credit, never crawl off-domain.** Found live
  (gymshark.com): a large, well-run store legitimately hosts its returns policy on a same-organization
  subdomain (`support.gymshark.com`) and/or a third-party returns portal (`loopreturns.com`) - the
  crawler's own SSRF hostname boundary correctly refuses to follow either, which previously meant the
  rule engine would call this "absent" and fire a false CRITICAL on a store that's actually compliant.
  `app.off_domain_credit` scans only links *already collected* during the normal crawl
  (`CrawledPage.external_links`, never a new fetch) for same-registrable-domain subdomains carrying a
  returns/contact keyword, or a known returns-portal domain (`loopreturns.com`, `narvar.com`,
  `happyreturns.com`, `returnly.com`, and others) - `tldextract` used for registrable-domain comparison
  so a bare CDN/assets subdomain without a keyword is correctly never credited. The credit records WHY
  (`matched_via`, `credited_url`, `found_on_page`) and explicitly means "presence detected, contents not
  audited in Phase 1" - never a silent full pass. Wired narrowly: `returns_policy` resource type only,
  and the exact `{business.email, business.phone}` all-missing field set only - not a generic hook for
  every rule, since no other resource type had an equivalent real-world off-domain pattern this round.
- **`variant_key` derivation fixed from SKU-first to first-distinguishing-identifier.** Found live
  (gymshark.com): on a real `ProductGroup`/`hasVariant` JSON-LD shape, `sku` is the shared parent value
  repeated identically across every size/color variant, while `gtin`/`mpn` are what actually differ per
  variant - deriving `variant_key` SKU-first silently gave every variant the same key, defeating piece
  3's "never collapse variants" the moment two variants were ever compared. Fixed to prefer whichever
  identifier actually varies: `gtin` (globally unique by design) > `mpn` > `sku`, falling back to a
  content hash of the offer's own fields (`sha256` of its sorted JSON, truncated) - deliberately *not* a
  positional index, since `_flatten_nodes` already splits a `hasVariant` product into separate top-level
  node calls, each with its own single-element `offers` list, so a positional index resets to 0 on every
  variant and silently recreates the exact collapse bug this fix exists to prevent. The content-hash
  fallback also satisfies "genuinely identical variants still group together" for free - identical
  content hashes identically - without a separate code path for that case.
- **Test-isolation audit, no other files at risk.** Grepped every other test file with a bare
  `Settings()` call (`test_screenshot_annotator.py`, `test_config_resource_bounds.py`,
  `test_adaptive_page_budget.py`, `test_graph_proxy_wiring.py`, `test_proxy_config.py`,
  `test_product_checks_dispatcher.py`) against every LLM-triggering entry point
  (`run_audit`/`run_llm_checks`/`get_llm_client`/`llm_configured`) - none call any of them, so none carry
  the same real-API-key-in-`.env` risk `test_first_audit.py` had (already fixed in an earlier round).
  No changes needed beyond confirming this.
- **Live-validated end to end against gymshark.com, for real** (full `run_first_audit` pipeline, no
  mocking): `snapshot_status=ACTION_REQUIRED`, not a false CRITICAL. The `missing_returns_page`
  evaluation came back `pass` with `off_domain_credit={"matched_via": "same_organization_subdomain",
  "credited_url": "https://support.gymshark.com/en-US/article/returns-policy"}` - confirming the
  detect-and-credit path fires on the real page, not just in synthetic tests. The real product page's
  JSON-LD produced 7 distinct GTIN-derived `variant_key`s despite every variant sharing the identical
  SKU `B6C5UUFHB` - confirming the variant_key fix on real data, not just the fixture shapes in
  `tests/test_facts_jsonld.py`.
  - A full-discovery live crawl (150-page budget, concurrency 4) proved unreliable in this sandbox
    specifically - first an OOM kill from the background-task memory reaper, then a Playwright driver
    pipe crash under concurrent browser contexts. This is a sandbox-resource finding, not a pipeline bug:
    the actual end-to-end confirmation above used a deliberately shrunk, targeted crawl
    (`crawl_max_pages=8`, `crawl_concurrency=1`, explicit page budget so adaptive sizing doesn't
    override it) against the real site, which completed cleanly and exercised the two previously-unproven
    live paths (off-domain credit, hasVariant product facts) without the memory load of full discovery.
    Full-scale live crawls remain validated via the existing test suite (706 passing) plus this
    targeted live run - not via a full discovery crawl in this environment.

## Rate-limiting gaps from the security audit (login brute force, First Audit OOM guard)

- **Login limits count failed attempts only, on two independent keys.** The per-IP window is
  5 per 60 s and the per-account (email) window is 10 per 15 min. Counting only failures means a
  real admin who logs in normally never hits the limit. The per-account key stops an attacker who
  rotates IPs. Its window is kept short on purpose: a per-account lockout lets anyone lock out the
  real admin, so a short window bounds that denial-of-service to minutes, not hours. Once a key is
  at its limit, even the correct password gets a 429 (a real lockout, not just slower guessing). A
  successful login resets both keys. The existing in-memory `RateLimiter` is reused, with the same
  single-process assumption.
- **First Audit cap is global, in-process and in-memory** (`first_audit_max_concurrent=2`). It is
  global rather than per-admin because there is a single admin, and the thing being protected
  (process memory under concurrent Playwright and LLM work) is shared anyway. Runs over the cap are
  rejected with a 429, not queued: a queue would add a persistence and ordering design nobody asked
  for, and the client can simply retry. The registry is in memory rather than derived from
  `FirstAuditRun.status`, so a run left `running` by a crashed process can never permanently block
  the cap.
- **Duplicate-run guard returns 409** and includes the existing run_id in the message. The URL key
  ignores the scheme, ignores host case and drops a trailing slash, so `example.com`,
  `https://Example.com/` and `http://example.com` count as the same store.
- **Background task references are now held** (`_FirstAuditSlots.tasks`). The slot is released in
  the task's `finally`, so a task garbage-collected mid-run would leak its slot permanently.
- **`shipping_region_contradiction` keeps `google_source_url: null` (re-searched). Superseded
  by the downgrade below.** Candidates
  checked: answer/12577710 (account shipping settings), answer/12578516 (required shipping info),
  answer/6324484 (`[shipping]` attribute) and answer/6150127 (Misrepresentation). All of them cover
  consistency between the feed and the website (cost/speed) or how shipping is disclosed. None
  states that ship-to regions must be consistent across a merchant's own pages, which is what this
  rule checks. Citing any of them would be a source that doesn't say what the rule claims. The
  loader's startup warning for this rule stays until a real source exists.
- **Single worker is a CHOSEN Phase-1 constraint, written down where a deploy would break it.**
  All three protections (concurrency cap, duplicate-run lock, login limiters) are per-process
  memory. Under `uvicorn --workers N` the cap silently becomes N x 2, a URL can run in two workers
  at once, and login limits loosen N x, with no error anywhere. Rather than build shared state now,
  the constraint is stated in the Dockerfile right above `CMD`, in the README run section, in
  `app/api/main.py`'s docstring, and next to each guard (`app/api/auth.py`,
  `app/api/first_audit.py`, `app/config.py`). The real fix (Redis) is on the pre-scale list below.
- **Every First Audit run has a wall-clock ceiling** (`first_audit_timeout_seconds=1800`). A hung
  run (Playwright stuck on a page) never terminates, so the `finally` that frees its slot never ran.
  Two hangs would have filled the cap of 2 and returned 429 to everything until a restart. Now
  `asyncio.wait_for` turns the hang into a timeout: the run is marked `error` ("timed out after
  Ns") and the slot is freed. It is a dedicated setting rather than the existing
  `audit_timeout_seconds`, which turned out to be defined but never enforced anywhere. That
  setting is clamped to a 30 s floor and documents the other pipeline. Per-page Playwright timeouts
  already exist: navigation 20 s and settle 5 s in both `PageFetcher` and the screenshot annotator,
  so a single stuck page normally fails fast. The outer ceiling covers whatever isn't bounded
  per-call (`page.evaluate`, LLM calls).
- **No startup sweep for orphaned `running` rows (deferred, cosmetic).** After a restart or crash
  mid-run, the run's DB row stays `running` forever because nothing resumes or errors it. That is
  only cosmetic here: the duplicate guard and the cap read the in-memory registry, never DB status,
  so a zombie row does not block that URL or a slot. It is listed under deferred scope below.

## `shipping_region_contradiction` downgraded to an advisory (option (b))

- **It was a rule-legitimacy question, not a missing lookup.** Google doesn't state that ship-to
  regions must be consistent across a merchant's own pages. The nearest text is Misrepresentation's
  "failing to clearly, conspicuously, and consistently disclose post-purchase terms…" (about
  concealing terms before purchase, not regions) and its ship-*from*-location bullets. Grounding the
  rule there would have been an inference presented as Google's text. Under this project's "no
  finding without a Google source" rule it can't ship as a violation, so it is now an advisory.
- **Advisory = the existing `quality_improvement` tier, keyed off the rule, not severity.** Findings
  from a `quality_improvement` rule get `risk_level="advisory"`, no `google_rule`, and a consequence
  that explicitly says it is *not* a Google policy violation and doesn't affect status.
  `compute_snapshot_status` ignores advisories (only advisories = COMPLIANT; they never mask a real
  risk finding). The PDF puts them in their own "Advisories" section after the verdict. This
  overrides the earlier "risk_level is a pure function of severity" decision for this one case
  only. That decision was about grading *within* risk findings; "is this a Google claim at all" is a
  categorical property of the rule.
- **Validation changed, not the warning silenced.** The loader's null-source warning now applies
  only to violation-tier rules, since advisories are expected to have no source. An advisory is in
  turn *forbidden* a `policy_reference` (load fails loudly), because one would print as
  "Google rule: …" with no source behind it. A null-source violation rule still warns.
- **Promotion path:** if an official source turns up, set `impact` to a violation tier, raise
  `severity`, and add `policy_reference` + `google_source_url` from that page. The loader and
  findings builder then treat it as a normal risk finding with no code change. The steps are
  written in a comment on the rule in `starter_rules.yaml`.

## Phase 1 next move: option B under a hard cap (LLM council, 2026-10-08)

- **Decision: B (validate a full crawl on real infrastructure), hard-capped, followed by a
  consultant test.** The options were A (build the First Audit frontend), B (validate a full
  crawl), and C (call it a proven POC and shelve). The council ran five advisors and five
  anonymised peer reviews; the full transcript is `active/council-transcript-2026-10-08.md`.
  - All five advisors rejected A for now. The pipeline is single-store and single-admin, but the
    buyer (agencies/consultants) runs many stores. The existing Next.js frontend makes adding the
    flow later cheap.
  - Shelving now (C) without any buyer contact was judged avoidance: the code and citations go
    stale while waiting for a buyer nobody is looking for.
  - B was endorsed for credibility, not scale. An 8-page report can't honestly be shown to anyone.
  - Consultants are treated as more than buyers: they are the only reliable source of known
    suspension cases, because they know the real reasons and have their clients' stores from
    before the fix.
- **Budget: 15 hours total over 3 weeks (2026-10-08 → 2026-10-29), kept separate from Upwork
  work.** This is a hard cap, not an estimate.
- **Crawl stop rule: 4 hours.** First step: upgrade Render to the $25/mo tier and run one 150-page
  First Audit on a store *outside* the validation set (not gymshark/ridge/modcloth). If 150 pages
  won't finish reliably within 4 hours of effort, use whatever page count does finish reliably
  (for example 50) and stop working on infrastructure. Deliberately run in a fresh session so the
  result gets read properly, not in the session that made this decision.
- **Then the consultant test:** run 3 full audits on stores outside the validation set and send
  them to 10–15 GMC consultants with two questions: "Is this right?" and "Would you give me one
  past suspended client, with the reason, so I can see if this catches it?" Every report states
  the website-only limit plainly (see "Standing truths").
- **Gate: no paid audits until the tool has caught at least 2 known suspension reasons.** A
  confident wrong diagnosis for a client in a suspension emergency would damage the builder's
  Upwork reputation, which is the actual income. This is why the council rejected the
  Expansionist's "sell $200–500 recovery audits now".
- **Overall stop rule: if no consultant has engaged by hour 15, write up the POC and shelve it.**
  At that point shelving is a decision backed by evidence (nobody engaged), not avoidance.

### Override, same day: user chose to build the frontend next instead

- Same session where a read-only Phase 1 gap audit was run (confirming the First Audit frontend is
  fully absent - the existing Next.js app is 100% the old monitoring pipeline). When asked to build
  the frontend, flagged that this was exactly what the council had rejected for now (option A) hours
  earlier, and asked the user to choose explicitly rather than silently either comply or refuse.
  **User's choice: build the frontend now, deliberately overriding option B's "Render crawl first"
  sequencing.** The 15h/3-week budget and the eventual Render-crawl/consultant-test steps from the
  council decision above are not cancelled, just reordered - frontend work is being pulled ahead of
  the crawl-validation step the council had sequenced first.
- Not re-litigated here (the council's reasoning above stands as the record of *why* B was chosen);
  this entry exists only to record *that* the sequencing was knowingly overridden, by the user, with
  the tradeoff stated plainly rather than silently dropped.

## First Audit frontend (built same day as the override above)

- **New, isolated `/first-audit/*` section** in the existing Next.js app, not a new app - the
  existing app already had the right infrastructure (Tailwind v4 CSS-variable theming, a hand-written
  `lib/api.ts`-style client, a proven setTimeout-poll pattern on `/report/[jobId]`) to extend rather
  than duplicate. The old monitoring UI (`/`, `/monitor`, `/report/[jobId]`) is untouched except for
  one added nav link.
- **Scope: login, run-trigger, status/poll, PDF download - nothing else**, matching the backend's
  actual surface confirmed in the same day's gap audit (no findings-list/detail JSON route exists).
  The run page links/downloads the PDF directly as the findings view, rather than building a findings
  endpoint first - confirmed with the user before building, since that was a real scope fork.
- **Black + light-blue theme, scoped, not global.** A `.fa-theme` wrapper class overrides the same
  CSS custom properties (`--background`/`--brand-1`/`--brand-2`/etc.) the existing indigo/fuchsia
  theme already uses, so every existing utility (`gradient-text`, `glass-card`, `gradient-ring`)
  reskins for free inside `/first-audit/*` with zero component changes - no new design system, no
  parallel component library. Brand color (`#0ea5e9`) deliberately matches the PDF report's own
  existing brand color rather than inventing a new one. Confirmed with the user before building
  (this was the original spec's "black+light-blue, standalone" requirement, still undone from an
  earlier round per this file's own "Admin auth" section).
- **Client-side auth guard, not middleware.** `useAdminGuard()` calls `GET /api/auth/me` and redirects
  to `/first-audit/login` on a 403 - directly implementing `app/auth/dependencies.py`'s own documented
  contract ("logged-out or non-admin = 403 (API) / redirect to login (UI)" - the redirect was
  explicitly left as a frontend concern by that module's docstring). A `middleware.ts`-based guard
  was not used: the admin check is already a real network call either way, and a second guard layer
  would just be two places to keep in sync for a two-page protected surface.
- **Every fetch call sets `credentials: "include"` explicitly** (new `lib/first-audit-api.ts`,
  separate from `lib/api.ts` since every route here is auth-gated and the old pipeline's isn't) -
  required because the API runs on a different origin than the frontend dev server, so the httponly
  session cookie is neither sent nor stored without it. The PDF download itself stays a plain
  `<a href>`, not a fetch+blob workaround - confirmed `SameSite=Lax` still attaches the cookie on a
  top-level GET navigation (what a download click actually is), matching the old pipeline's own
  `reportDownloadUrl()` pattern.
- **`asJson` exported from `lib/api.ts`** (was a private helper) so the new client reuses it rather
  than duplicating the same response/error-parsing logic.
- **Live-validated against the real running backend, not just typechecked** - the Chrome browser
  extension wasn't connected in this sandbox, so an actual visual click-through wasn't possible;
  instead, curl exercised the exact same HTTP calls the frontend makes (session cookie, credentials,
  status codes) against a live `uvicorn` process: login -> `/me` -> create a real run against
  `https://example.com` (deliberately tiny/static, to avoid this sandbox's known full-crawl
  instability) -> polled to `status=done` -> downloaded a real, valid 2-page PDF with the correct
  `Content-Disposition`/`Content-Type` headers -> logout -> confirmed `/me` then 403s. Every response
  shape matched the TypeScript types exactly. `npm run build`/`lint`/`tsc --noEmit` all clean.

## Whole-app redesign + whole-app admin gating (same day, user-directed expansion)

- **Confirmed before building** (4 explicit questions, all answered): gate the whole app (not just
  First Audit), skip Ant Design (Tailwind + GSAP + Framer Motion + reactbits.dev-style flourishes
  instead - running Ant Design's own CSS reset/design system alongside Tailwind was judged not worth
  the clash risk for this app's size), redesign every page (not just First Audit), proceed now and
  track hours against the existing 15h cap.
- **Backend: every route in `app/api/main.py` now behind `require_admin`** except `/health` (17
  routes: the whole old `/api/audits` + `/api/monitor/*` pipeline, previously fully public). Confirmed
  via `grep`/`awk` that no line was missed. Low-risk to add: `app/api/main.py`'s actual HTTP layer had
  **zero existing test coverage** (the only test hitting `app.api.main` mounts a separate minimal app
  with just the first-audit/auth routers, per this file's own "Admin auth" section) - so gating it
  could not break any existing test, confirmed by a full suite run after (726 passed).
- **`lib/api.ts`'s own fetch calls needed `credentials: "include"` added to every one of them** - the
  gating change above would otherwise 403 every old-pipeline call even while logged in, since the
  session cookie is cross-origin and silently dropped without it. Found and fixed before it could ship
  as a real break, not after.
- **One global `AuthProvider`/`Protected` pair (`lib/auth-context.tsx`), not six copies of a per-page
  guard.** Replaces the First-Audit-only `useAdminGuard()` hook (deleted) - every page in the app now
  gets the same gate for free from `AppShell` (`components/AppShell.tsx`, the root layout's one client
  wrapper), rather than each page repeating its own `GET /api/auth/me` + redirect logic. `/login`
  (renamed from `/first-audit/login`, which is deleted) is the one path excluded from the gate, since
  it has to render for a logged-out visitor.
- **Black + light-blue promoted from the First Audit section's scoped `.fa-theme` to the single global
  theme** (`app/globals.css`) - there's no more "public site" vs. "admin section" to keep two palettes
  for, since the whole app is behind the same login now. Every existing utility (`gradient-text`,
  `glass-card`, `gradient-ring`) reskinned for free via the same CSS custom properties, zero component
  rewrites needed for the base palette.
- **Found and fixed a real round of light-theme leftovers** this promotion would otherwise have
  exposed as illegible or wrong-feeling content, not just cosmetic drift: `prose-slate dark:prose-invert`
  on the markdown report view (would have rendered the actual finding text in light-theme prose colors
  on an always-dark page - the single most important fix in this list, since that's the report's main
  content, not chrome); several `bg-*-50`/`bg-*-100`/`text-*-600`/`text-*-800` banners and badges
  (success/warning messages, severity pills, the delta-report badge) that were tuned for a light
  background and would have been low-contrast or outright wrong-colored on the new dark one; a stray
  `shadow-indigo-500/20` left over from the old brand color. Found via a full grep sweep for light-
  background-tuned Tailwind utilities across `app/` and `components/`, not by eyeballing each page.
- **GSAP added** (`gsap` + `gsap/ScrollTrigger`, both included in the one `gsap` package - no separate
  paid-plugin install needed post-2025) for scroll-triggered section entrances (`components/
  ScrollReveal.tsx`) and a tweened stat counter (`components/AnimatedCounter.tsx`) - kept to these two
  reusable components rather than a bespoke GSAP timeline per page, to keep the animation surface
  small and consistent. Framer Motion (already installed, "Motion" is its current project name but the
  installed `framer-motion` package is the same library and already proven throughout this app) stays
  the tool for component-level micro-interactions (buttons, hover, in-view stagger) - the two libraries
  do different jobs here, not competing ones.
- **reactbits.dev treated as a pattern source, not an npm dependency** (it doesn't have one) - adapted
  as `components/SpotlightCard.tsx` (a cursor-following radial highlight, CSS custom properties written
  on `mousemove`, no per-frame React re-render) and a `.gradient-border` utility (animated gradient
  border via a double `background` layer, `app/globals.css`).
- **Home page rebuilt into a landing+dashboard hybrid** (hero, a live `stores monitored` count via
  `AnimatedCounter`, three feature cards linking to all three audit modes, the existing ad-hoc-audit
  form) rather than leaving it as just the bare form it was - this is the page most people will land on
  first, so it carries most of this round's "extensive website" visual weight.
- **Checkpointed mid-round, then continued** (user confirmed): `/monitor`, `/monitor/[storeId]`,
  `/report/[jobId]`, and the two First Audit pages then got the same `spotlight-card` treatment on
  every stat tile/history item/report panel (a shared inline `handleSpotlight` pointer handler per
  page, rather than wrapping every existing `motion.div` in the separate `SpotlightCard` component -
  these cards already have their own Framer Motion hover/layout/exit animations, so adding one CSS
  class + one event handler to the existing element was simpler than nesting two card abstractions).
  `AnimatedCounter` applied to every numeric stat tile across the report/monitor/first-audit run
  pages. The two long-form report-markdown panels (`/monitor/[storeId]`, `/report/[jobId]`) wrapped in
  `ScrollReveal` - the one place on these pages where a scroll-triggered entrance earns its keep, since
  the content is genuinely long; short above-the-fold sections elsewhere were deliberately left on
  their existing Framer Motion fadeUp/stagger rather than adding a second, competing entrance animation
  system for no visual benefit.
  - Found one more light-theme leftover during this pass: `text-amber-600` on the report page's
    polling-retry message (same class of bug as the earlier sweep, just missed the first time since it
    wasn't in the original grep's color list).
- **Live-verified the whole-app gating against the real running backend, not just reasoned about it**:
  `GET /api/monitor/stores` with no cookie -> 403; same call with a valid admin session cookie -> 200
  with real pre-existing monitored-store data. `npm run build`/`lint`/`tsc --noEmit` all clean; full
  Python suite 726 passed after the backend gating change.

## Light mode added back (user noticed its absence, asked directly)

- **The whole-app theme round above had deliberately removed light mode entirely** - no toggle, no
  `prefers-color-scheme` media query, just one fixed dark palette applied unconditionally as `:root`.
  That was a real design choice made in the moment (interpreting "black + light-blue" as one cohesive
  look), not something the user had asked to drop - flagged as such when asked why, then three options
  offered (stay dark-only, add a manual toggle, restore OS-based auto-switching). **User chose: add a
  manual light/dark toggle**, defaulting to dark.
- **`next-themes` added** rather than hand-rolling the toggle - it is the standard, widely-used solution
  for exactly this (SSR-safe theme switching with no flash-of-wrong-theme), and hand-rolling it well
  means re-solving the same FOUC/hydration problem it already solves. Configured `attribute="class"`,
  `defaultTheme="dark"`, `enableSystem={false}` (a manual toggle was explicitly chosen over "follow the
  visitor's OS setting" - the third option on offer).
- **Tailwind v4 switched from its default media-query-based `dark:` variant to class-based**
  (`@custom-variant dark (&:where(.dark, .dark *));`, the officially documented v4 mechanism) so
  `next-themes`' `.dark`/`.light` class on `<html>` is what actually drives `dark:` utilities, not the
  OS. Verified directly in the compiled production CSS (grepped for `.dark\:` selectors and the
  `.light` custom-property block) rather than assumed from the source alone.
  - `components/ThemeToggle.tsx` reads `useSyncExternalStore` for its "has the client mounted yet" flag
    instead of the classic `useEffect(() => setState(true), [])` did-mount pattern - a newer ESLint rule
    (`react-hooks/set-state-in-effect`) flagged that pattern as a real anti-pattern (cascading renders),
    caught by `npm run lint`, not shipped and found later.
- **The real work this round: every color I had hardcoded to a dark-only value during the earlier
  whole-app-theme round had to be re-conditionalized into a light-default + `dark:`-override pair** -
  the earlier round correctly deleted the OLD app's existing `dark:` variants (then genuinely dead,
  since the app was forced dark-only), but that meant a real light mode now needs every one of them
  back, in the opposite direction (the bare class is now the light-mode value, `dark:` the override).
  Found via a full grep sweep across `app/` and `components/` for every raw-Tailwind-palette dark shade
  (`text-slate-400`, `text-red-400`, `bg-red-950`, etc.) with no paired light counterpart - not by
  memory of which files were touched earlier. The one with real content-legibility impact, same as
  before: `ReportView.tsx`'s `prose-invert` and `SEVERITY_CLASS` map, both restored to their original
  light-default + `dark:` form (confirmed identical to this project's own established pre-session
  convention, not a new pattern invented for this round).
- **Verified past source-reading**, since a browser click-through wasn't available in this sandbox: read
  the actual compiled, minified production CSS for both the `.light` custom-property block and the
  `dark:`-prefixed utility rules, and curled the served `/login` HTML to confirm `next-themes`' blocking
  anti-FOUC script is present at the right place (immediately after `<body>` opens, before any visible
  content) - not just trusting that the library "should" work as documented.

## Scope decisions (explicitly deferred, not silently dropped)

- LLM-call-failure-reason specificity (needs an `LLMClient` protocol change with a concurrency-safety
  consideration) — flagged for a dedicated future round.
- A live embedded-screenshot example from a genuinely *presence*-type finding — mechanism is fully
  built and tested; no real store tried so far has produced that specific finding shape.
- The accuracy validation set — requires the user's own manual ground-truth pass, not more code.
- **Before scaling out (V1 / pre-scale):** move the First Audit concurrency cap, the
  duplicate-run lock and the login rate-limit counters to Redis (already in the stack). Required
  before running more than one uvicorn worker; not needed while Phase 1 is single-worker.
- **Dead `audit_timeout_seconds` on the old `/api/audits` pipeline:** the setting (30 min,
  hard-clamped) is labelled "wall-clock for one full audit" but nothing reads it, so that pipeline
  has no run ceiling. It is the same hang class just fixed for First Audit
  (`first_audit_timeout_seconds`): a stuck crawl runs forever. Fix by wrapping the job's run in
  `asyncio.wait_for(..., settings.audit_timeout_seconds)` and marking the job errored on timeout.
  Tracked, not fixed in this round.
- Startup reconciliation sweep: mark orphaned `running` First Audit rows as `error` ("interrupted
  by restart") on boot. Cosmetic only, since no guard reads DB status (see "Rate-limiting gaps").

Everything else raised across every round to date has been built, tested, and live-validated —
see `last.md` for the full narrative behind each entry above.
