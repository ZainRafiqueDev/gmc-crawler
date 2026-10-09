# LLM Council transcript: Phase 1 next move (2026-10-08)

Outcome: **B under a hard cap**. Logged in `decisions.md` ("Phase 1 next move"). The standing
truth about precision/recall/website-only that came out of this council is recorded in
`decisions.md` ("Standing truths about the product") and at the top of `README.md`.

Method: five advisors with different thinking styles answered independently; their responses
were anonymised (A–E) and each of five reviewers scored them; a chairman synthesised the verdict.
Anonymisation key: A = The Executor, B = The Contrarian, C = The Expansionist, D = The Outsider, E = The First Principles Thinker.

## Framed question

**Decision:** What is the best next move for a solo-built "GMC Compliance Monitor"? It crawls an e-commerce store and reports Google Merchant Center policy-compliance risks with cited official Google sources. The options:
- (A) build the frontend;
- (B) validate a full-scale crawl on real infrastructure first;
- (C) call the backend a proven POC, write it up, and move on until there's a concrete buyer.

**State (from the user):**
- **Phase 1 backend complete:** discovery → fact extraction (regex + JSON-LD + LLM-assisted prose) → contradiction engine → rule engine → findings → compliance snapshot status → evidence storage → admin auth → PDF report. 720 automated tests pass.
- **Live-validated** on gymshark.com, ridge.com and modcloth.com. The first live round found four false positives on well-run stores; all were root-caused and fixed.
- **Every violation finding is sourced.** Each finding that claims a Google violation cites a verified official Google page. The one rule that couldn't be sourced was downgraded to a no-violation advisory.
- **Security basics are in:** login brute-force limit, concurrent-audit cap, per-run timeout. Single-worker deployment is a documented constraint.
- **Not done:**
  1. There's no UI for this pipeline; it's reachable only via raw HTTP or FastAPI /docs.
  2. A full-size crawl has never completed. The dev sandbox runs out of memory or crashes Playwright at 150 pages, so all live validation used 8-page crawls.

**Facts from the codebase that the user's summary understates:**
- **A frontend already exists.** The repo has a Next.js frontend for an older audit pipeline (run-audit, report view, monitoring pages). So A means adding login and the First Audit flow to that app, not building a UI from scratch.
- **The crawler samples a store.** Default 150 pages, hard cap 500, max 200 product pages per category, max 50 products sampled for LLM checks. It never crawls 10k–100k products, so "scale" really means whether a 150–500-page crawl finishes within memory on a real box.
- **A previous deploy failed on memory.** On Render's free tier (512 MB RAM) it silently killed audit jobs mid-crawl. The cause (Chromium exhausting Docker's /dev/shm) was found in production logs and fixed. Render's $25/mo Standard tier is the first with real memory headroom.

**Context:**
- The intended buyer is agencies and GMC consultants managing many stores. Nothing multi-tenant exists: it's single-store, single-admin.
- The builder is a solo developer balancing freelance client work (Upwork), a CS degree and other builds. Phase 1's stated goal was "prove the compliance engine works." No buyer has been talked to.

**Stakes:** time is limited. The wrong call either wastes weeks polishing something nobody buys, ships a UI over an engine that falls over in production, or shelves a working asset that goes stale. The user asked for "the real tradeoff, not reassurance."

## Advisor responses

### Response A — The Executor
Do B, and do it this week. It's the only one of the three options with a clear first step that ends in a yes or no.

**Monday morning:** upgrade Render to Standard ($25), deploy the Docker image you already have, and run a 150-page First Audit on gymshark.com. Then run one at the 500 cap. Watch the memory graph and the logs. That's an evening of work, not a sprint. The /dev/shm fix is already in, so you're testing a fix you've already made, not starting a new investigation.

There are only two outcomes, and both pay off:
- **It finishes.** Now you have a real 150-page report on a real store. That PDF is your sales asset.
- **It OOMs.** You found out before you put a UI on top of it. The fix is usually limited: recycle the browser context every N pages, cap concurrency at 1–2, and close pages explicitly. That's days of work, not weeks.

**Why not A first:** wiring login and First Audit into the existing Next.js app is real work. Maybe a week or two of evenings, alongside Upwork and school. And every audit you'd click through that UI is an 8-page audit, because a full one has never finished. You'd be building a front door to a house you haven't inspected.

**Why not C:** "write it up and move on" has no next step attached. Writing it up is fine. Moving on with no buyer contact means the asset just decays, which is the exact risk you flagged.

**What follows B, in order:**
1. Run full-size audits on 3–5 stores (not your validation stores), ideally ones with known GMC suspensions, which you can find on Reddit and in the GMC forums.
2. Send those PDFs to 10 GMC consultants or agencies. Most freelance their services publicly on Upwork and LinkedIn.
3. Build the UI only if one of them says "I'd use this for my clients." The PDF plus FastAPI /docs is enough for a demo. Nobody buys or rejects a compliance tool over a login screen.

**Time box:** if the crawl can't be stabilized within one week, stop and reassess. Don't let B turn into an infrastructure project.

### Response B — The Contrarian
All three options share one flaw: each assumes the engine is proven. It isn't. Here is what nobody has checked.

**You've measured precision on the wrong population.** Gymshark, Ridge and ModCloth are well-run, non-suspended stores. Testing on them can only show you false positives. Your buyer's clients are stores that were already suspended or flagged, and you have zero data on false negatives there. A compliance tool that stays quiet on a store Google just suspended is worse than no tool, because the agency loses face with its client. Until you've run the engine on stores with a known suspension reason and checked it catches that reason, "proven POC" (option C) is a label you haven't earned. Write that up and you're documenting a guess.

**8-page crawls hide the actual risk.** Suspensions usually come from the long tail: one product page with a mismatched price, a missing return policy on one variant, a restricted-category item buried deep in the catalog. Eight pages samples the homepage and policy pages, which are exactly where well-run stores are clean. So B isn't just a memory test. It's the first time you'll see whether findings hold up across a real catalog sample.

**Unknown unit economics.** LLM extraction on 50 products, times many stores per agency, times monitoring frequency. You don't know the cost per audit. If it's $2 to $5 a run, agency pricing gets ugly.

**What are you competing with?** Merchant Center Diagnostics is free and comes straight from Google. Your edge has to be catching problems *before* Google flags them. You haven't shown that.

**Option A is the worst choice.** It wires a single-tenant, single-admin UI for a buyer who needs multi-tenant. You'd build it twice.

**What to do:** spend one afternoon and $25 to get a 150-page crawl through (B). Then find 5 to 10 stores that were actually suspended (GMC help forums and r/PPC are full of them, and they'll often name the reason). Run the engine on them, score hits and misses, and send those reports to 10 GMC consultants. Their reaction is your next decision. Shelving with no ground truth means the asset decays, and Google's policy pages drift, so your citations rot too.

### Response C — The Expansionist
All three options treat the engine as the product and miss what's actually valuable here. A crawler is a commodity. The rare part is a rule corpus where every violation claim cites a verified official Google source, plus a contradiction engine that catches when a store's shipping, returns or pricing statements disagree across pages. Agencies don't pay for a crawl. They pay to avoid account suspensions, and those cost real money: lost Shopping revenue, weeks of appeals, and clients who leave.

**The upside nobody is pricing in:**

1. **Suspension recovery, not monitoring.** A store that's just been suspended has an emergency, a deadline, and a budget. A "here's exactly why, with Google's own words, fix list attached" report is worth $200-500 a run to a panicking merchant or the consultant they hire. That's a better first wedge than recurring monitoring for agencies.
2. **The PDF report is already a sellable product.** Agencies would white-label it as a pre-onboarding audit for new clients, which makes it a sales tool for *them*. Multi-tenancy and a dashboard come later. One branded PDF per prospect store gets you to revenue first.
3. **You're on Upwork every day.** GMC suspension fix jobs get posted there all the time. Each one is a paying test case and a source of real failure data that tunes the rules. The distribution channel is already in your hands.
4. **The cited-rules layer can be reused.** Meta Commerce, Amazon and Microsoft Merchant all have the same suspension pain. If the rule engine is data-driven, each one is a new rules pack, not a new product.

**My pick isn't A, B or C.** Do B, because a $25 box and a few hours turns "8-page demo" into "audited a 300-page store end to end." Skip A for now. Then sell manual audits: run them yourself, deliver the PDF, and bid on 5 GMC suspension jobs this month. The existing Next.js frontend can wait until someone pays twice. C throws away what's most valuable here: a corpus of verified-citation rules that compounds with every real suspended store it sees.

### Response D — The Outsider
Coming in with no background, here's what stands out: none of the three options involves talking to the person who is supposed to pay.

All three (A, B, C) are about the builder's view of the product. To an outsider, "720 tests passing," "contradiction engine" and "cited official Google sources" sound like a lot of rigor. They don't tell me whether an agency would ever use this. The phrase that does tell me something is "single-store, single-admin." The buyer you've named runs many stores. So what you have is a tool for a customer you haven't described, sold to a customer it doesn't fit.

The second thing that's obvious only to you: your best evidence is invisible. You found four false positives on Gymshark-type stores and fixed them. That is a strong story, and right now it lives in FastAPI /docs and a PDF nobody has seen. A GMC consultant can't judge your engine. They can judge one report about a store they know.

My call is none of the options as written. It's a hybrid, in this order:

1. Do B, because it's cheap: about $25 and an afternoon. The reason isn't scale. You can't honestly show anyone a report from an 8-page crawl. Run one full crawl and you get one credible artifact.
2. Pick 5 to 10 real stores that have actually been suspended from Merchant Center (agencies post about these publicly), run the tool on them, and send the PDFs cold to 10 GMC consultants. Ask one question: "Would this have saved you time on this client?"
3. Don't touch the frontend until someone says yes. The UI already exists, so wiring it later is cheap. Building it now for a single-store tool you'd then have to make multi-tenant is the actual waste.

C ("write it up and move on") sounds disciplined. In practice it means never finding out. A writeup isn't a buyer test. Ten emails with ten real reports are.

The test of whether this is real: if nobody among 10 consultants replies to a free report about their own kind of problem, that's your answer, and you got it in a week, not a quarter.

### Response E — The First Principles Thinker
You're asking the wrong question. A, B and C are all answers to "what do I do with the software next?" The thing you don't know isn't about the software. It's this: **will an agency pay to have this risk found for them?** Not one of your 720 tests checks that. Phase 1 proved the engine works. It proved nothing about whether anyone wants it.

Start from what an agency actually buys. They don't want a crawler or a dashboard. They want to stop getting suspended, and they want something credible to hand their client. Your output already does that job: a PDF in which every violation cites an official Google page. That PDF is the product. Everything else is how it gets delivered.

So the real next step isn't on your list. **Sell the report by hand.** Pick 10 to 20 GMC consultants or agencies, many of whom post on Upwork, where you already work. Run audits on their clients' stores yourself, through /docs, and send them the PDF. Ask them three things: is it right, would you pay for it, and how much. Your own hands are the frontend. A human delivering the report is the cheapest MVP you have.

What this does to your options:
- **A (frontend):** wrong for now. A login screen for one admin, who is you, answers nothing. Multi-tenant is what agencies would need, and you don't know yet whether that's worth building.
- **B (scale validation):** partly right, but only as plumbing. Spend the ~$25 and the hours so you can run audits for prospects without hitting OOM. Do it because a real buyer is waiting on a real audit, not as a standalone milestone.
- **C (shelve):** sounds like discipline but is really avoidance. "Wait until there's a buyer" when you're doing nothing to find one means the code decays while you wait.

The test: in 3 weeks, did anyone pay, or ask for a second audit? If yes, build what they asked for. If no, then you've earned the right to shelve it.

## Peer reviews (anonymised; reviewers saw only letters)

### Peer review 1
1. Strongest is B. It's the only response that questions whether the engine actually works, not just how big it can run. Testing on gymshark, ridge and modcloth can only turn up false positives. You have zero data on false negatives, the cases where a store with a real problem gets a clean report. B also raises cost per audit and the free competitor (Merchant Center Diagnostics), which the others ignore. A, C and D mention suspended stores, but only as sales demos, not as a way to measure what the tool misses.
2. Biggest blind spot is C. It prices suspension-recovery reports at $200–500 and pitches Meta and Amazon rule packs. That rests on a rule corpus nobody has checked against a single suspended store. E is close behind: it sends prospects reports from 8-page crawls and treats B as plumbing.
3. What all five missed: many GMC suspensions (misrepresentation, price or availability mismatch) come from conflicts between the Merchant Center product feed and the site's pages. A crawler that sees only the website can't detect them. Also, the "suspended stores from Reddit and forums" plan breaks down: by the time you crawl those stores, most have already fixed the problem. They can't show what the tool catches unless you have an old snapshot of the site (for example, Wayback Machine) or a consultant's client data from before the fix.

### Peer review 2
1. Strongest: B. The only response that questions whether the engine actually works, not just whether it runs. Nobody has measured false negatives (missed violations) on stores that were actually suspended, which is the number an agency cares about. B also raises cost per audit for the LLM work, the free Merchant Center Diagnostics tool as the real competitor, and Google policy pages changing so the citations go stale.
2. Biggest blind spot: C. It prices suspension-recovery audits at $200-500 and tells the builder to bid on paid Upwork suspension jobs now, selling to panicking clients with an engine that has no data on what it misses. One missed suspension reason on a paid job hurts the builder's Upwork reputation, which is their actual income. A has a milder version: its plan for "3-5 suspended stores" never says to score hits against misses.
3. What all five missed: many GMC suspensions come from comparing the product feed against the landing page, or from account-level checks. A crawler with no feed access has a hard ceiling. Agencies have feed access, so accepting a feed export as input may matter more than a 150-page crawl. None of the five raised anti-bot blocking or rate limits on 150-500 page crawls either.

### Peer review 3
1. Strongest: B. The only response that questions whether the engine actually works, not only whether anyone wants it. Recall on suspended stores is completely untested. It also brings up cost per audit, Merchant Center Diagnostics, and citation rot. Its plan is concrete: score hits and misses on stores with a known suspension reason.
2. Biggest blind spot: C. Selling $200-500 reports to merchants in the middle of a suspension when the engine has never been checked on one suspended store; a confident wrong diagnosis in an emergency costs reputation fast. It also wanders into Meta/Amazon/Microsoft before one GMC sale exists. A treats B purely as a memory problem.
3. What all five missed: many suspensions come from the product feed, not the site, so a site-only crawler's recall ceiling may be low by design; feed (or Content API) access is the actual competitive question. Forum-sourced suspended stores are unreliable ground truth: many have already fixed the problem, and Google often gives only vague reasons such as "misrepresentation".

### Peer review 4
1. Strongest: B. It questions the evidence itself; raises unknown LLM cost and Merchant Center Diagnostics as competitor. Its claim that A means "building twice" is a bit overstated, but its main point holds.
2. Biggest blind spot: C. It makes up a price and a market, then says sell manual audits now, ignoring the recall gap; a miss on a paying emergency client hurts the builder's Upwork reputation. A is close behind: it re-tests on gymshark, a validation store, and calls both outcomes wins even though a finished crawl proves nothing about value.
3. What all five missed: the crawler can't see the feed (feed-vs-site price/availability mismatches, GTINs, account-level misrepresentation signals); forum "suspended stores" are weak ground truth because they've often already changed their pages; nobody weighed the opportunity cost against paid Upwork hours.

### Peer review 5
1. Strongest: B. The only one that questions the evidence itself; its plan ends in a scored result (hits and misses on suspended stores), not just a reaction from consultants.
2. Biggest blind spot: C. Selling "verified citations" by hand before knowing what the engine misses risks the builder's reputation and the Upwork channel C relies on.
3. What all five missed: suspended stores are weak ground truth (already fixed by the time they're posted; reasons often vague "misrepresentation"); the tool only sees the website, while many suspensions come from the feed, account history or billing; no one set a cost limit in hours on the builder's own time.

### Facts verified in the codebase by the session (after the reviews)
- Confirmed: the engine has NO Merchant Center feed / Content API ingestion anywhere. The closest proxy is the `price_mismatch_jsonld_vs_platform` rule, which compares a product page's JSON-LD price against the platform's own API (Shopify/WooCommerce) — a stand-in for feed-vs-site mismatch only when the feed is auto-generated from that platform.
- Anti-bot blocking IS already handled: the crawler detects bot-protection/CAPTCHA/rate-limit pages and reports those pages as "could not verify", never as a confirmed pass or a confirmed absence (a fix from the first live-testing round, after a Cloudflare block page was being read as content). So reviewer 2's "nobody raised anti-bot" is a real-infra risk that is detected and disclosed, not unhandled — though block rates at 150-500 pages on real infra are still unmeasured.

## Chairman's verdict

### Where the Council Agrees

- **Not the frontend, not yet.** All five advisors reject building the UI now. The pipeline is single-store and single-admin, but the buyer runs many stores. Building a UI first means building it for the wrong user, and an existing Next.js shell makes wiring it up later cheap.
- **Do the 150-page crawl, keep it small.** Every advisor endorses B as a $25, few-hour step. The reason is credibility, not scale. An 8-page report is not something you can honestly show a consultant.
- **Shelving now is avoidance.** Writing it up and moving on (option C) without any buyer contact means the code and the Google citations go stale while you wait for a buyer you aren't looking for.
- **The PDF is the product.** The Executor, The Outsider and The First Principles Thinker each landed on this independently. Send real reports to GMC consultants and let their reaction decide what you build next.

### Where the Council Clashes

- **Does the engine work, or does anyone want it?**
  - The Contrarian says recall is unproven. You've only measured false positives on clean stores, and a tool that stays quiet on a store Google just suspended is worse than no tool.
  - The First Principles Thinker says demand is the only unknown and B is just plumbing.
  - Both are right about different risks. The Contrarian's risk is the one that hurts your reputation.
- **Sell now, or validate first?**
  - The Expansionist wants paid suspension-recovery audits ($200-500) and Upwork bids this month.
  - Every peer review flagged this as the most dangerous advice. A confident wrong diagnosis on a client in a suspension emergency damages your Upwork reputation, which is your actual income.
- **What B is for.** The Executor treats it as a memory test. The Contrarian treats it as the first time you'll see whether findings hold up across a real catalog. The Contrarian is right: long-tail product pages are where suspensions come from.

### Blind Spots the Council Caught

- **The tool can't see the feed.** Many suspensions (price or availability mismatches, GTINs, account-level misrepresentation) come from the Merchant Center feed or the account, not the website. The codebase confirms there is no feed or Content API ingestion. `price_mismatch_jsonld_vs_platform` is only a partial stand-in. Site-only recall has a ceiling built in.
- **Forum-sourced suspended stores are weak ground truth.** Most have fixed their pages by the time you crawl them, and Google's stated reasons are often just "misrepresentation". The Contrarian's scoring plan needs pre-fix snapshots (Wayback Machine) or a consultant's own client data.
- **Your time costs money.** No advisor priced your hours against paid Upwork work or set a limit in hours.
- **Anti-bot blocking is detected but not measured.** Blocked pages already show up as "could not verify", but block rates at 150-500 pages are unknown.

### The Recommendation

Go with B, but cap it hard, then run a consultant test that measures recall and demand at the same time. I break with the majority's framing on one point: consultants aren't only buyers. They're the only reliable source of ground truth, because they know the real suspension reasons and have the before-fix state of their clients' stores.

- **Budget: 15 hours total, over 3 weeks, kept out of Upwork hours.**
- **Crawl stop rule: 4 hours.** If 150 pages won't finish on Render's $25 tier by then, ship whatever page count does finish reliably (for example 50 pages) and stop working on infrastructure.
- Run 3 full audits on stores that weren't in your validation set. Send them to 10-15 GMC consultants with two questions:
  - "Is this right?"
  - "Would you give me one past suspended client, with the reason, so I can see if this catches it?"
- Say in plain words in every report that the tool sees the website only, not the feed.
- **No paid audits** until the tool has caught at least 2 known suspension reasons.
- **Overall stop rule:** if by hour 15 no consultant has engaged, write the POC up and shelve it. At that point shelving is a decision backed by evidence, not avoidance.

### The One Thing to Do First

Upgrade Render to the $25 tier and run one 150-page First Audit on a store outside your validation set. Stop after 4 hours whether or not it finishes.
