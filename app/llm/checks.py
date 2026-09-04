"""Step 5: LLM-graded checks (Claude or OpenAI, per Settings.llm_provider -
see app/llm/factory.py). Every finding here cites which specific policy
requirement it's checking against, retrieved from the real RAG policy index
(app/llm/policy_rag.py - Phase C) built from live-scraped GMC Help Center
pages, with a real source URL + section cited per finding. Falls back to
the hand-written stub set (app/llm/policy_snippets.py) only if retrieval
returns nothing (no index built yet, no DB, or no OpenAI key) - logged as a
warning there, since it shouldn't normally happen once the index exists.

A page that passes a check produces no Finding (consistent with the
deterministic checks) - the report generator derives "pass" for a page from
the absence of findings against it.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re

from app.config import Settings
from app.db import Database
from app.llm.cache import LLMCache
from app.llm.client import LLMClient
from app.llm.factory import get_llm_client
from app.llm.policy_rag import get_policy_context
from app.llm.policy_snippets import get_snippet
from app.models import Confidence, CrawledPage, Finding, LLMCoverageStats, PageType, Severity, SiteMap

logger = logging.getLogger("gmc_audit.llm.checks")

_POLICY_PAGE_CHECKS: dict[PageType, str] = {
    PageType.PRIVACY_POLICY: "privacy_policy",
    PageType.SHIPPING_POLICY: "shipping_policy",
    PageType.RETURNS_POLICY: "returns_refunds",
    PageType.TERMS_OF_SERVICE: "terms_of_service",
}

_MAX_PRODUCT_PAGES_CHECKED = 5
_LLM_CONCURRENCY = 3
_PAGE_TEXT_LIMIT = 6000

# Fixed-size LLM catalog sampling follow-up, Part 1.3 (option C, user-confirmed
# 2026-09-03): sample size scales with catalog size instead of a flat 5, capped by
# Settings.llm_product_sample_cap (default 15, hard ceiling in app/config.py).
_PRODUCT_SAMPLE_FRACTION = 0.05
_PRICE_RE = re.compile(r"[$€£]\s?(\d{1,5}(?:[.,]\d{2})?)")
# Short product copy leaves little room to substantiate a claim or qualify a
# comparison - a cheap proxy for elevated editorial/prohibited-content risk,
# not a judgment on its own.
_THIN_CONTENT_WORD_THRESHOLD = 40

_LOCATION_FIELD = {
    "type": "string",
    "description": (
        "Which specific element/section of the page this refers to - e.g. 'returns policy section', "
        "'checkout page shipping line', 'main product description paragraph', 'footer'. Required, not optional; "
        "never fold this into the reasoning/evidence text instead."
    ),
}

_SUBSTANCE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "meets_requirement": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["confirmed", "potential_risk"]},
        "evidence_quote": {
            "type": "string",
            "description": "A short VERBATIM quote copied from the page text. Never invent or paraphrase a quote - if nothing relevant exists, return an empty string.",
        },
        "location": _LOCATION_FIELD,
        "reasoning": {"type": "string"},
        "recommended_fix": {"type": "string"},
    },
    "required": ["meets_requirement", "confidence", "evidence_quote", "location", "reasoning", "recommended_fix"],
}

_EDITORIAL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "has_quality_issue": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["confirmed", "potential_risk"]},
        "evidence_quote": {"type": "string", "description": "Verbatim quote of the problematic text, or empty string."},
        "location": _LOCATION_FIELD,
        "issue_description": {"type": "string"},
        "recommended_fix": {"type": "string"},
    },
    "required": ["has_quality_issue", "confidence", "evidence_quote", "location", "issue_description", "recommended_fix"],
}

_PROHIBITED_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "potentially_prohibited": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["confirmed", "potential_risk"]},
        "matched_category": {"type": "string", "description": "Which prohibited/restricted category this might fall under, or empty string."},
        "evidence_quote": {"type": "string", "description": "Verbatim quote of the concerning text, or empty string."},
        "location": _LOCATION_FIELD,
        "reasoning": {"type": "string"},
    },
    "required": ["potentially_prohibited", "confidence", "matched_category", "evidence_quote", "location", "reasoning"],
}

_CLAIM_CONTRADICTION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "has_contradiction": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["confirmed", "potential_risk"]},
        # Forces the model to name WHAT specifically conflicts, rather than
        # a bare yes/no - live testing found the bare yes/no alone was not
        # enough discipline: the model flagged "30-Day Free Returns" against
        # a policy whose quoted text was ordinary exceptions (damaged/used
        # items) for that SAME 30-day window, and separately flagged "free
        # shipping" against a policy that literally confirmed free shipping
        # on the same terms - i.e. verdicts that didn't actually conflict
        # with their own quoted evidence. Naming the conflict dimension
        # makes that kind of non-conflict harder to rationalize.
        "conflict_dimension": {
            "type": "string",
            "enum": ["cost", "timeframe_or_window", "eligibility_or_region", "availability", "none"],
            "description": (
                "Which specific dimension conflicts: the claim and policy state DIFFERENT costs, DIFFERENT "
                "timeframes/windows, DIFFERENT eligibility/region, or one says available and the other says "
                "not offered at all. Use 'none' if there is no genuine conflict (ordinary conditions/exceptions "
                "on an otherwise-matching claim, e.g. 'used/damaged items excluded' on a matching return window, "
                "are NOT a conflict - use 'none')."
            ),
        },
        "claim_quote": {
            "type": "string",
            "description": "A short VERBATIM quote of the claim from the product/homepage page. Empty string if no genuine contradiction.",
        },
        "policy_quote": {
            "type": "string",
            "description": "A short VERBATIM quote of the contradicting text from the policy page. Empty string if no genuine contradiction.",
        },
        "location": _LOCATION_FIELD,
        "reasoning": {"type": "string"},
        "recommended_fix": {"type": "string"},
    },
    "required": [
        "has_contradiction", "confidence", "conflict_dimension", "claim_quote", "policy_quote",
        "location", "reasoning", "recommended_fix",
    ],
}

# Part 3 of the follow-up round (claim-vs-policy contradiction check):
# cheap pre-filter so this only ever sends a page to the LLM when it
# actually makes a shipping/returns-adjacent claim worth comparing - most
# product/homepage pages don't, and an LLM call with nothing to compare
# would just be cost with no signal (same tiering principle as the rest of
# this module's docstring). Deliberately broad-but-cheap; the LLM prompt
# itself (not this regex) is what decides whether a genuine, specific
# contradiction exists - a false-positive pre-filter match just means one
# extra LLM call that then correctly finds nothing, not a wrong finding.
_SHIPPING_CLAIM_RE = re.compile(
    r"free shipping|ships? (worldwide|internationally|in \d+|same[- ]day|next[- ]day)|"
    r"shipping (is free|included)|fast shipping|express shipping|flat[- ]rate shipping",
    re.IGNORECASE,
)
_RETURNS_CLAIM_RE = re.compile(
    r"\d+[-\s]day(s)? (return|money[- ]back)|free returns?|no[- ]questions[- ]asked|"
    r"hassle[- ]free returns?|money[- ]back guarantee|full refund|returns? accepted|satisfaction guarantee",
    re.IGNORECASE,
)

_DAY_COUNT_RE = re.compile(r"(\d+)[-\s]?days?", re.IGNORECASE)


def _same_day_count_in_both(claim_quote: str, policy_quote: str) -> bool:
    """True when both quotes name the identical day count(s) (e.g. both say
    "30 days"/"30-day") - see check_claim_policy_contradiction's docstring
    for the real, reproduced false-positive pattern this guards against."""
    claim_days = set(_DAY_COUNT_RE.findall(claim_quote))
    policy_days = set(_DAY_COUNT_RE.findall(policy_quote))
    return bool(claim_days) and claim_days == policy_days

_ANTI_HALLUCINATION_SYSTEM_PREFIX = (
    "You are a compliance auditor. Every evidence_quote you return MUST be copied verbatim from the supplied "
    "page text - never invent, paraphrase, or reconstruct a quote from memory. If no relevant text exists, "
    "return an empty string for evidence_quote rather than fabricating one."
)

# Folded into check_prohibited_content's own prompt (not a separate check or
# category - counterfeit/brand-risk is already squarely inside the existing
# "prohibited content" policy area, matching how GMC's own policy text
# treats it: support.google.com/merchants/answer/6149970's Counterfeit
# Goods section). The watch-term list is deliberately guidance, not a
# keyword-match trigger - the model still has to find *supporting context*,
# not just a word's presence; a page that merely names a real brand (e.g.
# "compatible with Apple devices," "fits iPhone 14 case") is not evidence
# of counterfeiting on its own and must not be flagged from that alone.
_COUNTERFEIT_GUIDANCE = (
    "When screening for counterfeit/brand-risk specifically, watch for language like: replica, 1:1, mirror "
    "quality, AAA (as a quality-grade claim, not a battery size), knockoff, inspired by [brand] but sold as if "
    "it were the real thing, or unauthorized use of a brand's logo/trademark presented as if it were an "
    "official/licensed product. A brand name appearing on the page is NOT by itself evidence of counterfeiting - "
    "many entirely legitimate products reference a brand for compatibility, comparison, or accessory fit (e.g. "
    "\"compatible with iPhone 14,\" \"fits Dyson V8,\" \"works with Apple AirPods\"). Only flag when the page's own "
    "text gives a real, specific reason to suspect the product is not genuine or is being misrepresented as "
    "another brand's - never from a brand mention alone."
)


_WHITESPACE_RE = re.compile(r"\s+")
_NOTE_EVIDENCE_NOT_VERIFIED = "Evidence quote could not be independently verified as exact page text."


def _normalize_for_quote_match(text: str) -> str:
    """Whitespace-collapsed, HTML-entity-decoded, case-folded - permissive
    enough that a genuinely real quote (which can differ from the raw page
    text only in whitespace or entity encoding) still verifies, while text
    that was actually invented/paraphrased still won't match. The page text
    an LLM-graded check is shown (CrawledPage.main_content_text/.text) is
    already de-boilerplated plain text, not raw HTML, so entity-decoding is
    a defensive extra rather than the primary need here - whitespace
    collapsing and case-folding do most of the real work.
    """
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().lower()


def verify_evidence_quote(quote: str | None, source_text: str) -> bool:
    """True if `quote` is a real (normalization-tolerant) substring of
    `source_text` - the same text the check actually gave the model to
    grade - confirming the forced-schema "verbatim quote" requirement
    actually held for this specific finding's content, not just its shape.
    An empty/blank quote verifies trivially (True): a check whose
    evidence_quote came back empty and fell back to its own reasoning text
    (a legitimate, schema-allowed case - see e.g. check_policy_page_substance)
    makes no verbatim claim, so there is nothing to falsify. False means a
    genuine content-fidelity gap: the model's quote could not be located in
    what it was actually shown, e.g. analytical prose describing what a
    page is missing rather than a real quote from it (found live this
    round, on a real check_policy_page_substance result).

    Deliberately in-memory, not a live-DOM search like
    app.checks.screenshot_annotator's - this only needs to confirm the
    quote is real text within the *already-available* prompt-time page
    text, not find a renderable element to highlight/screenshot (a
    different job with a real live-browser dependency this one doesn't
    need).
    """
    quote = (quote or "").strip()
    if not quote:
        return True
    return _normalize_for_quote_match(quote) in _normalize_for_quote_match(source_text)


def _confidence_after_verification(model_confidence: Confidence, verified: bool) -> Confidence:
    """A quote that failed verification can never stand as CONFIRMED - the
    anti-hallucination guarantee it was supposed to rest on didn't hold for
    this specific finding. Downgrades to POTENTIAL_RISK (still reported,
    per this project's never-silently-discard pattern - see Finding.
    evidence_verified's docstring) rather than CANNOT_VERIFY, since the
    underlying judgment (meets_requirement/has_quality_issue/etc.) still
    came from a real model call that saw the real page text - only the
    verbatim-quote guarantee specifically is in question, not the whole
    verdict. Already-POTENTIAL_RISK stays as-is (nothing to downgrade to).
    """
    if not verified and model_confidence == Confidence.CONFIRMED:
        return Confidence.POTENTIAL_RISK
    return model_confidence


def _policy_reference(ctx) -> str:
    """A finding's policy_reference: includes real source-URL citations
    when the RAG index actually produced a hit, so a reader can go verify
    the finding against the real Google page directly - not just a bare
    policy name.
    """
    base = f"GMC policy: {ctx.title} [{ctx.id}]"
    if ctx.from_real_index and ctx.citations:
        return f"{base} - {'; '.join(ctx.citations)}"
    return base


_REQUIREMENT_EXCERPT_LIMIT = 600


def _requirement_excerpt(ctx) -> str | None:
    """The real retrieved policy text a finding was graded against
    (app/report.py's richer per-finding format renders this as "Specific
    Policy Requirement") - only when it's genuinely from the live RAG
    index, never the hand-written stub summary presented as if it were a
    real quote.
    """
    if not ctx.from_real_index or not ctx.summary:
        return None
    return ctx.summary[:_REQUIREMENT_EXCERPT_LIMIT]


async def check_policy_page_substance(
    client: LLMClient, page: CrawledPage, policy_id: str, settings: Settings, db: Database | None = None, cache: LLMCache | None = None,
) -> Finding | None:
    page_text = page.main_content_text or page.text or ""
    if not page_text:
        return None
    ctx = await get_policy_context(policy_id, page_text, settings, db, cache)
    if ctx is None:
        return None

    system = (
        f"{_ANTI_HALLUCINATION_SYSTEM_PREFIX} You are checking whether a webpage's text actually satisfies a "
        "specific Google Merchant Center policy requirement."
    )
    user = (
        f"Policy requirement [{ctx.id}] {ctx.title}:\n{ctx.summary}\n\n"
        f"Page URL: {page.url}\nPage text (may be truncated):\n{page_text[:_PAGE_TEXT_LIMIT]}\n\n"
        "Does this page's text meet the policy requirement above? Call the tool with your verdict."
    )
    result = await client.call_tool(system, user, "submit_policy_verdict", _SUBSTANCE_TOOL_SCHEMA)
    policy_reference = _policy_reference(ctx)

    if result is None:
        return Finding(
            check_id=f"llm_policy_substance_{policy_id}",
            title=f"Could not evaluate {ctx.title} content",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence="The LLM API call failed or returned no usable result.",
            policy_reference=policy_reference,
        )
    if result.get("meets_requirement"):
        return None

    evidence_quote = result.get("evidence_quote") or ""
    verified = verify_evidence_quote(evidence_quote, page_text)
    model_confidence = Confidence.CONFIRMED if result.get("confidence") == "confirmed" else Confidence.POTENTIAL_RISK

    return Finding(
        check_id=f"llm_policy_substance_{policy_id}",
        title=f"{ctx.title} page lacks required substance",
        severity=Severity.HIGH,
        confidence=_confidence_after_verification(model_confidence, verified),
        page_url=page.url,
        evidence=evidence_quote or result.get("reasoning", "(no evidence returned)"),
        policy_reference=policy_reference,
        policy_requirement_text=_requirement_excerpt(ctx),
        policy_last_verified=ctx.verified_at,
        recommended_fix=result.get("recommended_fix"),
        location=result.get("location"),
        from_cache=result.get("_from_cache", False),
        evidence_verified=verified,
    )


async def check_editorial_quality(
    client: LLMClient, page: CrawledPage, settings: Settings, db: Database | None = None, cache: LLMCache | None = None,
) -> Finding | None:
    page_text = page.main_content_text or page.text or ""
    if not page_text:
        return None
    ctx = await get_policy_context("editorial_quality", page_text, settings, db, cache)
    if ctx is None:
        return None

    system = f"{_ANTI_HALLUCINATION_SYSTEM_PREFIX} You are reviewing a storefront page for professional editorial quality."
    user = (
        f"Policy requirement [{ctx.id}] {ctx.title}:\n{ctx.summary}\n\n"
        f"Page URL: {page.url}\nPage text (may be truncated):\n{page_text[:_PAGE_TEXT_LIMIT]}\n\n"
        "Does this page have a significant editorial/quality issue (spelling/grammar errors, placeholder text "
        "like 'Lorem ipsum', broken/nonsensical content, or content reading as clearly auto-generated/low-effort)? "
        "Minor stylistic quirks do not count. Call the tool with your verdict."
    )
    result = await client.call_tool(system, user, "submit_editorial_verdict", _EDITORIAL_TOOL_SCHEMA)
    policy_reference = _policy_reference(ctx)

    if result is None:
        return Finding(
            check_id="llm_editorial_quality",
            title="Could not evaluate editorial quality",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence="The LLM API call failed or returned no usable result.",
            policy_reference=policy_reference,
        )
    if not result.get("has_quality_issue"):
        return None

    evidence_quote = result.get("evidence_quote") or ""
    verified = verify_evidence_quote(evidence_quote, page_text)
    model_confidence = Confidence.CONFIRMED if result.get("confidence") == "confirmed" else Confidence.POTENTIAL_RISK

    return Finding(
        check_id="llm_editorial_quality",
        title="Editorial/professional quality issue found",
        severity=Severity.MEDIUM,
        confidence=_confidence_after_verification(model_confidence, verified),
        page_url=page.url,
        evidence=evidence_quote or result.get("issue_description", "(no evidence returned)"),
        policy_reference=policy_reference,
        policy_requirement_text=_requirement_excerpt(ctx),
        policy_last_verified=ctx.verified_at,
        recommended_fix=result.get("recommended_fix"),
        location=result.get("location"),
        from_cache=result.get("_from_cache", False),
        evidence_verified=verified,
    )


async def check_prohibited_content(
    client: LLMClient, page: CrawledPage, settings: Settings, db: Database | None = None, cache: LLMCache | None = None,
) -> Finding | None:
    page_text = page.main_content_text or page.text or ""
    if not page_text:
        return None
    ctx = await get_policy_context("prohibited_content", page_text, settings, db, cache)
    if ctx is None:
        return None

    system = (
        f"{_ANTI_HALLUCINATION_SYSTEM_PREFIX} You are screening product page text for prohibited/restricted "
        f"product content. {_COUNTERFEIT_GUIDANCE}"
    )
    user = (
        f"Policy requirement [{ctx.id}] {ctx.title}:\n{ctx.summary}\n\n"
        f"Page URL: {page.url}\nPage text (may be truncated):\n{page_text[:_PAGE_TEXT_LIMIT]}\n\n"
        "Does this product page's text suggest a potentially prohibited or restricted product category? "
        "Call the tool with your verdict."
    )
    result = await client.call_tool(system, user, "submit_prohibited_content_verdict", _PROHIBITED_TOOL_SCHEMA)
    policy_reference = _policy_reference(ctx)

    if result is None:
        return Finding(
            check_id="llm_prohibited_content",
            title="Could not screen product content",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence="The LLM API call failed or returned no usable result.",
            policy_reference=policy_reference,
        )
    if not result.get("potentially_prohibited"):
        return None

    category = result.get("matched_category") or "unspecified category"
    evidence_quote = result.get("evidence_quote") or ""
    verified = verify_evidence_quote(evidence_quote, page_text)
    model_confidence = Confidence.CONFIRMED if result.get("confidence") == "confirmed" else Confidence.POTENTIAL_RISK

    return Finding(
        check_id="llm_prohibited_content",
        title=f"Potentially prohibited product content ({category})",
        severity=Severity.CRITICAL,
        confidence=_confidence_after_verification(model_confidence, verified),
        page_url=page.url,
        evidence=evidence_quote or result.get("reasoning", "(no evidence returned)"),
        policy_reference=policy_reference,
        policy_requirement_text=_requirement_excerpt(ctx),
        policy_last_verified=ctx.verified_at,
        recommended_fix="Review this product listing manually and remove/revise if it violates GMC product policies.",
        location=result.get("location"),
        from_cache=result.get("_from_cache", False),
        evidence_verified=verified,
    )


_CLAIM_TYPE_LABEL = {"shipping": "shipping/delivery", "returns": "returns/refund"}


async def check_claim_policy_contradiction(
    client: LLMClient, claim_page: CrawledPage, policy_page: CrawledPage, claim_type: str, settings: Settings,
    db: Database | None = None, cache: LLMCache | None = None,
) -> Finding | None:
    """Part 3 of the follow-up round: a marketing claim on a product/
    homepage page ("free shipping," "30-day returns," "ships worldwide")
    versus what the store's own shipping/returns policy page actually
    says - genuinely uncovered by every existing check (confirmed before
    building this: check_business_identity_consistency only cross-checks
    contact-identity fields - email/phone/address - never claim language;
    the price/stock checks compare product-page data against a platform
    API, never against policy-page text). Needs semantic comparison, not
    exact string matching, so this is LLM-graded like every other check
    here - same forced-schema, verbatim-both-sides-evidence pattern.

    Grounded via the "misrepresentation" policy area regardless of whether
    the comparison is against a shipping or returns policy page - a claim
    that contradicts the store's own stated policy is the same category of
    issue as an inconsistent business-identity field (already grounded
    that way), not a shipping/returns-policy-substance question on its own.

    claim_type ("shipping" or "returns") topic-locks the comparison to the
    SAME policy dimension the policy_page is actually about - live testing
    against a real store found that without this, a page carrying both a
    shipping badge and a returns badge (a common trust-badge row - "Free
    Shipping | 30-Day Returns | Secure Checkout") let the model quote a
    SHIPPING claim as "contradicting" a RETURNS policy page's text (or vice
    versa), a topic mismatch that isn't a contradiction of anything.

    Deliberately conservative (Part 3.3), hardened after live testing found
    real false positives from an earlier, less strict version of this
    prompt: flagged "30-Day Free Returns" against a policy whose quoted
    text was ordinary exceptions (used/damaged items) for that SAME 30-day
    window - not a real conflict, just normal fine print; separately
    flagged "free shipping" against a policy page that literally confirmed
    free shipping on the same terms - a verdict that contradicted its own
    quoted evidence. Two things now guard against that: (1) the prompt
    below explicitly names both real failure patterns as non-examples, and
    (2) the schema forces a structured `conflict_dimension` field (cost /
    timeframe / eligibility / availability / none) rather than a bare
    yes-no, making it harder to rationalize a non-conflict as "true".

    On top of the prompt itself, a code-level anti-hallucination gate:
    BOTH claim_quote and policy_quote must be non-empty verbatim text, not
    just one side, AND conflict_dimension must not be "none" - a
    "contradiction" finding can't stand on a quote from only one of the
    two pages being compared, or on a verdict that names no real conflict.

    Unlike the other checks here, an LLM call that fails/returns nothing
    produces no Finding at all (not a CANNOT_VERIFY placeholder) - this
    check is opportunistic on top of an already-thin pre-filter match, not
    a required-page-type check where "we couldn't check this" is itself
    worth surfacing to the reader.
    """
    claim_text = claim_page.main_content_text or claim_page.text or ""
    policy_text = policy_page.main_content_text or policy_page.text or ""
    if not claim_text or not policy_text:
        return None

    ctx = await get_policy_context("misrepresentation", claim_text, settings, db, cache)
    if ctx is None:
        return None

    topic = _CLAIM_TYPE_LABEL[claim_type]
    system = (
        f"{_ANTI_HALLUCINATION_SYSTEM_PREFIX} You are checking whether a {topic} marketing claim on a storefront "
        f"page genuinely contradicts what the store's own {topic} policy page states. Only evaluate {topic} "
        f"claims - ignore any other kind of claim on the page (e.g. if you are checking returns, ignore shipping "
        f"claims entirely, and vice versa) even if both appear together in the same trust-badge row.\n\n"
        "Be conservative. Two real failure patterns to avoid, found in earlier testing of this exact check:\n"
        "1. Ordinary conditions/exceptions on an otherwise-MATCHING claim are NOT a contradiction. Example: a "
        "claim of \"30-day returns\" next to a policy that also gives a 30-day window but adds normal fine "
        "print (\"used, damaged, or missing-parts items may be denied or partially refunded\") is CONSISTENT, "
        "not contradictory - real returns policies always have exclusions like this.\n"
        "2. A policy quote that actually CONFIRMS or agrees with the claim is NOT a contradiction, even if it "
        "isn't verbatim-identical wording. Example: a claim of \"free shipping\" next to a policy stating "
        "\"we offer free shipping ($0.00) on all orders\" is CONSISTENT - re-read your own quoted policy text "
        "before deciding it disagrees with the claim.\n"
        "Only flag a SPECIFIC, genuine disagreement where the claim and the policy cannot both be true - "
        "different cost, a shorter/different timeframe, narrower eligibility/region, or the policy saying the "
        "thing isn't offered at all. A vague reference (\"see our shipping policy for details\") is also not a "
        "contradiction."
    )
    user = (
        f"Claim page ({claim_page.url}) text (may be truncated):\n{claim_text[:_PAGE_TEXT_LIMIT]}\n\n"
        f"Policy page ({policy_page.url}) text (may be truncated):\n{policy_text[:_PAGE_TEXT_LIMIT]}\n\n"
        f"Does the claim page make a specific {topic} claim that genuinely contradicts the policy page? Call the "
        "tool with your verdict, quoting both the claim and the contradicting policy text verbatim if so, and "
        "naming which specific dimension conflicts."
    )
    result = await client.call_tool(system, user, "submit_claim_contradiction_verdict", _CLAIM_CONTRADICTION_TOOL_SCHEMA)
    if result is None or not result.get("has_contradiction"):
        return None
    if result.get("conflict_dimension") == "none":
        logger.debug("Claim-contradiction verdict on %s said has_contradiction but conflict_dimension=none - discarding", claim_page.url)
        return None

    claim_quote = (result.get("claim_quote") or "").strip()
    policy_quote = (result.get("policy_quote") or "").strip()
    if not claim_quote or not policy_quote:
        # Anti-hallucination gate on top of the prompt itself: a genuine
        # contradiction needs both sides quoted verbatim - a verdict
        # without one is not trustworthy enough to report as a finding.
        logger.debug("Claim-contradiction verdict on %s missing a verbatim quote on one side - discarding", claim_page.url)
        return None

    if result.get("conflict_dimension") == "timeframe_or_window" and _same_day_count_in_both(claim_quote, policy_quote):
        # Deterministic backstop on top of the prompt: live testing found
        # prompt wording alone was NOT enough - even with an explicit
        # negative example naming this exact scenario, gpt-4o-mini still
        # verdicted "timeframe_or_window" conflict on "30-Day Free Returns"
        # against a policy quote that ALSO says "30-day return window" (its
        # own quoted evidence agrees with the claim; only the surrounding
        # exception language - "used/damaged/missing parts" - differs, which
        # is normal fine print, not a timeframe conflict). Reproduced on 5
        # of 5 real product pages on a real store even after the prompt
        # fix, so a wording-only fix was not sufficient here - when both
        # quotes name the identical day count, this specific conflict
        # dimension cannot be genuine regardless of what the model concluded.
        logger.debug(
            "Claim-contradiction verdict on %s claimed a timeframe conflict but both quotes name the same day "
            "count - discarding as a known model failure pattern", claim_page.url,
        )
        return None

    dimension = result.get("conflict_dimension") or "unspecified"
    # Two quotes, two source texts - each verified against the specific page
    # it's claimed to be verbatim from, not against the other page or the
    # combined text (a claim_quote that's real text on the policy page but
    # not the claim page would be exactly the kind of mix-up this is meant
    # to catch, not something a combined check should paper over).
    verified = verify_evidence_quote(claim_quote, claim_text) and verify_evidence_quote(policy_quote, policy_text)
    model_confidence = Confidence.CONFIRMED if result.get("confidence") == "confirmed" else Confidence.POTENTIAL_RISK

    return Finding(
        check_id="llm_claim_policy_contradiction",
        title=f"Product/homepage claim contradicts the store's own policy page ({dimension.replace('_', ' ')})",
        severity=Severity.HIGH,
        confidence=_confidence_after_verification(model_confidence, verified),
        page_url=claim_page.url,
        evidence=f'Claim on {claim_page.url}: "{claim_quote}" — contradicts {policy_page.url}: "{policy_quote}"',
        policy_reference=_policy_reference(ctx),
        policy_requirement_text=_requirement_excerpt(ctx),
        policy_last_verified=ctx.verified_at,
        recommended_fix=result.get("recommended_fix") or "Align the claim with the store's actual stated policy, or update the policy page to match the claim.",
        location=result.get("location"),
        from_cache=result.get("_from_cache", False),
        evidence_verified=verified,
    )


_MAX_CLAIM_PAGES_CHECKED = _MAX_PRODUCT_PAGES_CHECKED


async def _claim_contradiction_tasks(
    client: LLMClient, site_map: SiteMap, settings: Settings, db: Database | None, cache: LLMCache | None,
) -> list:
    """Builds the (unbounded-concurrency-wise, caller wraps in `bounded`)
    list of check_claim_policy_contradiction coroutines for this site -
    homepage + up to _MAX_CLAIM_PAGES_CHECKED product pages, each screened
    against whichever of the store's shipping/returns policy page(s) it has
    a pre-filter-matched claim for. Nothing queued at all if the store has
    neither a shipping nor a returns policy page reachable - there is
    nothing to compare a claim against.
    """
    shipping_policy_pages = [p for p in site_map.pages_of_type(PageType.SHIPPING_POLICY) if p.reachable]
    returns_policy_pages = [p for p in site_map.pages_of_type(PageType.RETURNS_POLICY) if p.reachable]
    if not shipping_policy_pages and not returns_policy_pages:
        return []

    claim_candidate_pages: list[CrawledPage] = []
    homepage = next((p for p in site_map.pages if p.page_type == PageType.HOMEPAGE and p.reachable), None)
    if homepage:
        claim_candidate_pages.append(homepage)
    claim_candidate_pages.extend([p for p in site_map.pages_of_type(PageType.PRODUCT) if p.reachable][:_MAX_CLAIM_PAGES_CHECKED])

    coros = []
    for page in claim_candidate_pages:
        text = page.main_content_text or page.text or ""
        if not text:
            continue
        if shipping_policy_pages and _SHIPPING_CLAIM_RE.search(text):
            coros.append(check_claim_policy_contradiction(client, page, shipping_policy_pages[0], "shipping", settings, db, cache))
        if returns_policy_pages and _RETURNS_CLAIM_RE.search(text):
            coros.append(check_claim_policy_contradiction(client, page, returns_policy_pages[0], "returns", settings, db, cache))
    return coros


def _extract_price(page: CrawledPage) -> float | None:
    text = page.main_content_text or page.text or ""
    match = _PRICE_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _product_sample_size(total_reachable: int, cap: int) -> int:
    return min(cap, max(_MAX_PRODUCT_PAGES_CHECKED, math.ceil(total_reachable * _PRODUCT_SAMPLE_FRACTION)))


def _select_product_sample(product_pages: list[CrawledPage], cap: int) -> list[CrawledPage]:
    """Risk-weighted pick of which product pages get the LLM editorial-
    quality/prohibited-content check, replacing a flat first-N-crawled
    selection (fixed-size LLM catalog sampling follow-up, Part 1.3 option C).
    Two cheap signals, both already available from the crawl with no new
    external data or brand watchlist: a price that's an outlier-cheap
    *within this store's own catalog* (no absolute threshold - "cheap" is
    relative to what a store normally sells, same reasoning the counterfeit-
    screening prompt already uses for not trusting an absolute signal), and
    thin product copy. Ties keep crawl order, so behavior stays deterministic
    across runs given the same crawl.
    """
    sample_size = _product_sample_size(len(product_pages), cap)
    if len(product_pages) <= sample_size:
        return product_pages

    entries = [(i, page, _extract_price(page)) for i, page in enumerate(product_pages)]
    # Rank-based (not value-based) bottom decile: with many exact-tie prices
    # (common on real catalogs - lots of $X.99 items), a value threshold
    # would over-match every page at that value instead of just the
    # genuinely cheapest few. Undefined unless there's enough of a sample to
    # make "bottom decile" meaningful.
    priced = sorted((price, idx) for idx, _, price in entries if price is not None)
    cheap_rank_cutoff = max(1, math.ceil(len(priced) * 0.1)) if len(priced) >= 5 else 0
    cheap_indices = {idx for _, idx in priced[:cheap_rank_cutoff]}

    def risk_rank(entry: tuple[int, CrawledPage, float | None]) -> tuple[int, int]:
        idx, page, _price = entry
        word_count = len((page.main_content_text or page.text or "").split())
        price_risk = idx in cheap_indices
        thin_risk = 0 < word_count < _THIN_CONTENT_WORD_THRESHOLD
        return (-(int(price_risk) + int(thin_risk)), idx)

    ranked = sorted(entries, key=risk_rank)
    return [page for _, page, _ in ranked[:sample_size]]


async def run_llm_checks(
    site_map: SiteMap, settings: Settings, cache: LLMCache | None = None, db: Database | None = None,
) -> tuple[list[Finding], LLMCoverageStats]:
    """Tiered checking (hardening round, section 2.2): LLM grading only ever
    runs on page types where it adds real judgment value over the
    deterministic checks - policy pages (substance check), the homepage and
    product pages (editorial quality), product pages (prohibited-content
    screening), and - homepage/product pages that pre-filter-match a
    shipping/returns claim, against the store's own policy page (Part 3 of
    the follow-up round: claim-vs-policy contradiction). This is
    deliberate, not incidental: collection/cart/checkout/contact/FAQ/blog
    pages are never sent to the LLM here, because the deterministic checks
    already cover what matters on those page types and an LLM call would
    just be cost with no added signal.

    db is for the real RAG policy index (app/llm/policy_rag.py) and is
    intentionally independent of cache: falls back to cache.db when not
    given explicitly, so callers that only ever have the two bundled
    together (the frontend/API path, always) don't need to change - but a
    caller that disables LLM result caching (audit.py's --no-cache) without
    also having no DB at all can still pass db explicitly to keep RAG
    retrieval working.
    """
    total_product_pages = len([p for p in site_map.pages_of_type(PageType.PRODUCT) if p.reachable])

    if not settings.llm_configured:
        findings: list[Finding] = []
        for page_type, policy_id in _POLICY_PAGE_CHECKS.items():
            for page in site_map.pages_of_type(page_type):
                if not page.reachable:
                    continue
                snippet = get_snippet(policy_id)
                findings.append(Finding(
                    check_id=f"llm_policy_substance_{policy_id}",
                    title=f"{snippet.title if snippet else policy_id} content not evaluated",
                    severity=Severity.LOW,
                    confidence=Confidence.CANNOT_VERIFY,
                    page_url=page.url,
                    evidence=f"API key not configured for LLM_PROVIDER={settings.llm_provider!r} - LLM-graded content checks were skipped.",
                    policy_reference=f"GMC policy: {snippet.title if snippet else policy_id}",
                    location=None,
                ))
        # No product page ever gets an editorial/prohibited-content check
        # run against it when the LLM isn't configured - previously silent
        # (no placeholder finding for these, unlike the required-policy-page
        # checks above), now surfaced via coverage stats instead of a
        # per-page CANNOT_VERIFY flood (product catalogs can be huge).
        return findings, LLMCoverageStats(llm_configured=False, total_reachable_product_pages=total_product_pages, product_pages_checked=0)

    client = get_llm_client(settings, cache)
    if db is None and cache is not None:
        db = cache.db
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def bounded(coro):
        async with sem:
            return await coro

    tasks = []

    for page_type, policy_id in _POLICY_PAGE_CHECKS.items():
        for page in site_map.pages_of_type(page_type):
            if page.reachable:
                tasks.append(bounded(check_policy_page_substance(client, page, policy_id, settings, db, cache)))

    homepage = next((p for p in site_map.pages if p.page_type == PageType.HOMEPAGE and p.reachable), None)
    if homepage:
        tasks.append(bounded(check_editorial_quality(client, homepage, settings, db, cache)))

    all_product_pages = [p for p in site_map.pages_of_type(PageType.PRODUCT) if p.reachable]
    product_pages = _select_product_sample(all_product_pages, settings.llm_product_sample_cap)
    for page in product_pages:
        tasks.append(bounded(check_editorial_quality(client, page, settings, db, cache)))
        tasks.append(bounded(check_prohibited_content(client, page, settings, db, cache)))

    coverage = LLMCoverageStats(
        llm_configured=True, total_reachable_product_pages=total_product_pages,
        product_pages_checked=len(product_pages),
    )

    for coro in await _claim_contradiction_tasks(client, site_map, settings, db, cache):
        tasks.append(bounded(coro))

    if not tasks:
        return [], coverage

    results = await asyncio.gather(*tasks, return_exceptions=True)
    findings = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("LLM check task raised: %s", r)
            continue
        if r is not None:
            findings.append(r)
    return findings, coverage
