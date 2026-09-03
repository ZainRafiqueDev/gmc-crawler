# Live Test Run — Crawl Robustness Round

Real, live test of this round's crawl-robustness work (Parts 1–5 of the follow-up brief, plus opt-in proxy support) against 13 user-supplied real store URLs. Run via the actual pipeline (platform detection → crawl → classify → deterministic checks + business-identity check), skipping LLM-graded checks for this pass (crawl/classification/reporting behavior is what's being validated here, not full compliance grading — happy to re-run any specific site with LLM grading for a complete report). Page budget capped at 20 pages/site (6 for `snocks.com`, see below) to keep the batch tractable — smaller than the 150-page production default, noted wherever it affects a result.

Full generated report for every site is in `reports/2026-08-31-crawl-robustness-test/`.

**Environment caveat, stated up front**: this test ran from one sandboxed session's network, not a real production deployment. Several sites failed with a network-level timeout/connection error rather than a real HTTP response — for some of those (`nike.com`, `decathlon.fr`) a plain `curl` from the same machine got further in earlier testing, meaning the failure is at least partly about *this specific test environment's* reachability to those hosts, not necessarily proof that the crawler itself would fail identically from a normal production network. Reported honestly as `network_error` either way, per the whole point of this round's Part 4.

---

## Results at a glance

| Site | Platform detected | Pages | Language | Result | Suspension-risk findings |
| --- | --- | --- | --- | --- | --- |
| britanniagifts.us | woocommerce | 20/20 reachable | en | ✅ Full crawl | 4 |
| allbirds.com | shopify | 14/20 reachable | en | ⚠️ Partial — 4 pages bot-blocked | 2 |
| meo.fr | woocommerce | 20/20 reachable | fr | ✅ Full crawl (i18n fix confirmed live) | 5 |
| velasca.com | shopify | 14/20 reachable | en | ⚠️ Partial — 6 pages network-error | 2 |
| snocks.com | shopify | 6/6 reachable (6-page budget) | de | ✅ Full crawl (smaller budget, see note) | 3 |
| byredo.com | unknown | 0 pages | — | ❌ robots.txt disallowed — honestly reported, not crawled | 0 |
| hellyhansen.com | woocommerce* | 0/1 | — | ❌ Total failure — network_error | 0 |
| solostove.com | unknown | 0/1 | — | ❌ Total failure — network_error | 0 |
| cutterbuck.com | unknown | 0/1 | — | ❌ Total failure — network_error | 0 |
| tiendanimal.es | unknown | 0/1 | — | ❌ Total failure — network_error | 0 |
| decathlon.fr | woocommerce* | 0/1 | — | ❌ Total failure — network_error | 0 |
| nike.com | unknown | 0/1 | — | ❌ Total failure — network_error | 0 |
| zalando.fr | woocommerce* | 0/1 | — | ❌ Total failure — bot_blocked (real HTTP 403) | 0 |

**\*** Platform-detection false positive, not this round's work — see "Pre-existing issue found, not fixed" below.

**Net: 5 of 13 sites crawled (fully or partially) with real findings; 8 failed to crawl at all** — and in **every one** of those 8 failures, the tool reported *why*, with zero false "missing page" claims. That's the headline result of this round: not a higher success rate on its own (this specific test environment has real reachability problems to several of these hosts), but **the honest-failure guarantee holding up under real, unplanned failures** — which is exactly what Part 4 was for.

---

## What worked — confirmed live

### Part 1/2 — retry/backoff + anti-bot interstitials
- **`allbirds.com`**: 4 of its `/collections/*` pages hit a genuine Cloudflare-style JS interstitial. The new wait-and-recheck step (§2.1 of this round) gave each one up to 6s to resolve — none did within that window, and each was correctly tagged `bot_blocked` (not a generic failure) and capped at 2 attempts instead of retried 3 full times. Evidence text: *"A bot-protection interstitial (e.g. a Cloudflare-style JS challenge) did not resolve after waiting 6s"*, with the recommended fix *"Ask the merchant to allowlist this tool's User-Agent (or IP)..."*.
- **`zalando.fr`**: first 2 attempts timed out at the network level, 3rd attempt got a real **HTTP 403**, correctly recognized and capped rather than retried a 4th time — confirms the 403-specific retry cap (Part 1.1) firing on a real response, not just in unit tests.
- **`snocks.com`** (first, larger-budget attempt, see log): showed the same interstitial detection firing repeatedly across `/de-ch/`, `/it-ch/`, `/fr-ch/` locale variants — real, repeated live confirmation of the challenge-detection logic engaging on genuinely different pages of the same real site.

### Part 3 — internationalization
- **`meo.fr`** (French WooCommerce): `<html lang="fr">` correctly detected on all 20 pages; `mentions-legales` and `conditions-generales-de-vente` correctly classified as Terms of Service (not `missing`); no false "unsupported language" downgrade since French has real coverage.
- **`snocks.com`** (German Shopify): `<html lang="de">` correctly detected across all 6 pages.
- No language-downgrade findings fired for either — meaning the classifier had genuine pattern coverage and didn't need the safety net, which is the good outcome (the safety net exists for languages *without* coverage).

### Part 4 — honest failure reporting
This is where the real-world value showed up most:
- **`byredo.com`**: robots.txt disallows the homepage. Report correctly says *"This tool did not attempt to crawl it"* — a respectful refusal, distinct from a failure, worded distinctly in both the banner and the finding evidence.
- **6 of the 13 sites** (`hellyhansen.com`, `solostove.com`, `cutterbuck.com`, `tiendanimal.es`, `decathlon.fr`, `nike.com`) failed at the homepage with a genuine network-level timeout. Every one produced exactly the "This Audit Could Not Run" banner + one honest `CANNOT_VERIFY` finding — **zero** false "Missing required page" claims, where the pre-round code would have produced up to 6 confident `CRITICAL` false negatives per site (30+ false findings across this batch alone, avoided).
- **`zalando.fr`**: same total-failure path, this time for a real `bot_blocked` reason rather than `network_error` — confirms the failure-category-specific wording flows through correctly for both categories.

### Part 5 — CAPTCHA/proxy
- No live CAPTCHA wall was hit in this batch (the `allbirds.com`/`snocks.com` interstitials were generic JS challenges, not CAPTCHA-specific — correctly *not* misclassified as `captcha_blocked`, which would have been a false claim).
- No proxy was configured for this run (none of the 13 total-failure sites had credentials to test against) — the total-failure sites remain exactly the kind of case §15.5 of `final.md` documents as the live evidence behind that feature.

---

## Real bugs found and fixed as a direct result of this test run

Three genuine issues surfaced by actually reading the generated reports, not assumed correct — same standard as the rest of this project:

1. **Final Assessment showed a clean "LOW risk, 100/100" score for a totally-failed crawl.** `byredo.com`'s report said *"This Audit Could Not Run"* at the top, then *"Overall risk level: LOW (Internal Audit Score: 100/100)"* near the bottom — a direct contradiction. Fixed: `_final_assessment` now shows *"Overall risk level: not applicable — this audit did not complete"* instead, whenever the crawl totally failed.
2. **Policy-by-Policy Review matrix showed "Pass" for all 8 policy areas on a totally-failed crawl** — the `crawl_incomplete` finding doesn't map to any of the 8 named policy areas, so with nothing else in `findings` the table looked like a clean sweep across the board. Fixed: every row now shows "Cannot Verify — Audit did not complete" instead when the crawl totally failed.
3. **The WooCommerce-REST-API recommendation banner still fired on a totally-failed crawl** — `zalando.fr`'s report (almost certainly not WooCommerce) showed *"Recommendation: connect the WooCommerce REST API..."* right next to "This Audit Could Not Run," because platform detection is a separate, independent probe that can false-positive from a WAF 403ing every path uniformly. Fixed: suppressed whenever the crawl totally failed.

All three are covered by new regression tests (`tests/test_crawl_failure_reporting.py`); full suite (366 tests) still green.

## Pre-existing issue found, not fixed (out of this round's scope)

**Platform detection false-positives "woocommerce" for at least 3 sites that are almost certainly not WooCommerce** (`hellyhansen.com`, `decathlon.fr`, `zalando.fr` — none are real WordPress/WooCommerce stores). All three show the same pattern: `/wp-json/wc/v3/products` returned `403`, and the existing heuristic reads "403 on that specific route" as "route exists, therefore probably WooCommerce" — but a WAF that 403s *every* path uniformly (including ones that don't exist) produces the same signal. This is a pre-existing `app/platform_detector.py` heuristic weakness, not something this round's brief covered (Parts 1–5 were about crawl/fetch/classification/reporting-honesty, not platform-ID accuracy) — flagging it here since it was found live in this exact test batch, worth a future round if you want it addressed. The one place it was actively misleading (the API-verification recommendation on a report that couldn't check anything) is fixed above; the underlying detection heuristic itself is not.

## Test-harness-only issues (not product bugs)

- `velasca.com` and `snocks.com` (at a 20-page budget) exceeded my *test script's* own artificial per-site timeout (150s) on the first pass — not a bug in the crawler, which has no such cap of its own (only the 30-minute whole-audit ceiling in `app/config.py`). Re-run with a longer harness allowance: `velasca.com` completed at 216.7s (14/20 reachable); `snocks.com` was re-run at a smaller 6-page budget for tractability and completed cleanly in 45s. Both are reflected correctly in the table above.

---

## Full reports

Every site's complete generated report is saved in `reports/2026-08-31-crawl-robustness-test/report-<site>.md` for direct inspection — page-by-page findings, exact evidence text, and the full Policy-by-Policy matrix for each.
