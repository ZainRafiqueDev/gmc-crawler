# Decisions Log

A running record of every non-obvious decision made on this project — what was chosen, what
alternatives existed, and why. This is a decision log, not a feature chronicle (`last.md` is the
full "what's implemented" narrative); an entry here exists because a real choice was made, usually
between two or more reasonable options, or because a real tradeoff was accepted knowingly.

**Convention going forward: every future decision on this project gets appended here**, in the
relevant section (or a new one), at the time it's made — not reconstructed after the fact.

---

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

## Scope decisions (explicitly deferred, not silently dropped)

- LLM-call-failure-reason specificity (needs an `LLMClient` protocol change with a concurrency-safety
  consideration) — flagged for a dedicated future round.
- A live embedded-screenshot example from a genuinely *presence*-type finding — mechanism is fully
  built and tested; no real store tried so far has produced that specific finding shape.
- The accuracy validation set — requires the user's own manual ground-truth pass, not more code.

Everything else raised across every round to date has been built, tested, and live-validated —
see `last.md` for the full narrative behind each entry above.
