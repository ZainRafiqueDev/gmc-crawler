"""Soft-404/catch-all detection (follow-up round - hypothesis-driven, not a
live-observed bug like every other fix in this project so far; see
decisions.md). HTTP 200 doesn't guarantee a page is genuinely distinct
content of its classified type - a soft-404 template or an SPA catch-all
route can return 200 for literally any URL. This module answers exactly one
question: "does this candidate page's content look like it's actually the
site's generic fallback content, rather than distinct real content?"

Core invariant (state this everywhere this module's result is used, not just
here): a strong match here may only ever *prevent* a page from being treated
as confirmed evidence of existence, downgrading a would-be CONFIRMED "exists"
to CANNOT_VERIFY. It must NEVER by itself produce a CONFIRMED/CRITICAL
"missing page" finding - see app.checks.deterministic.check_required_pages,
the only caller. Ambiguous evidence always means CANNOT_VERIFY, never
"missing".

Deliberately not a new class hierarchy (no PageVerification/PageIdentity/
RequiredPageResolver) - one small, targeted addition to the existing
CANNOT_VERIFY/failure-category framework. Deliberately fully deterministic -
no LLM/semantic similarity is used or should ever be added here; see
app.llm.checks and app.graph for confirmation the LLM layer never
participates in page-existence decisions at all.

Reuses, rather than reinvents:
- app.change_detection's content-hashing (compute_content_hash /
  normalize_for_content_hash) for the exact-match tier.
- app.checks.duplicate_products's near-duplicate threshold (0.92,
  difflib.SequenceMatcher) for the near-identical tier - that number has
  already been exercised against real stores in this project; inventing a
  second, uncalibrated threshold here would repeat a mistake this project
  has already corrected once (see decisions.md's note on fabricated
  numeric weights).

Match hierarchy (only the first two tiers are strong enough to stand alone -
see the module docstring above and decisions.md for why a third,
title+H1-based tier is deliberately not implemented as an independent
trigger this round):
1. Exact content-hash match -> strongest signal, sufficient alone.
2. Near-identical normalized text (SequenceMatcher ratio >= the same 0.92
   threshold duplicate_products.py already uses) -> strong signal,
   sufficient alone. In practice this also implies matching title/H1 for
   real templates, so it subsumes the "same title + same H1 + highly
   similar body" tier the original brief described as merely a supporting
   signal, without needing a second invented threshold for it.
Anything weaker (partial wording similarity alone, without either of the
above) is NOT sufficient and must not trigger a downgrade.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from app.checks.duplicate_products import _NEAR_DUPLICATE_THRESHOLD


def is_strong_content_match(
    candidate_normalized_text: str | None,
    candidate_content_hash: str | None,
    baseline_normalized_text: str | None,
    baseline_content_hash: str | None,
) -> bool:
    """True only when the candidate's content strongly matches the baseline
    (a known-nonexistent-URL probe, or the homepage - see
    check_required_pages) - the caller's signal to treat the candidate as
    NOT confirmed-distinct content, never as confirmed-missing (see this
    module's docstring). False whenever the baseline itself is unavailable
    (e.g. the nonexistent-URL probe failed this run) - soft-404 detection
    against that baseline degrades to a no-op rather than blocking anything.
    """
    if not baseline_content_hash or not candidate_content_hash:
        return False
    if candidate_content_hash == baseline_content_hash:
        return True
    if baseline_normalized_text and candidate_normalized_text:
        ratio = SequenceMatcher(None, candidate_normalized_text, baseline_normalized_text).ratio()
        if ratio >= _NEAR_DUPLICATE_THRESHOLD:
            return True
    return False
