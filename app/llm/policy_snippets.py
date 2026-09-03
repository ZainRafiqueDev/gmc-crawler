"""Phase 1 stub for the GMC policy knowledge base every LLM finding must
cite. Phase 2 replaces this with a real pgvector RAG index over live GMC
Help Center pages (weekly re-embed job) - see project brief. For now this
is a small, hand-curated set of policy summaries good enough to ground the
LLM-graded checks and give every finding a real citation instead of a
hallucinated one.
"""
from __future__ import annotations

from pydantic import BaseModel


class PolicySnippet(BaseModel):
    id: str
    title: str
    summary: str


POLICY_SNIPPETS: list[PolicySnippet] = [
    PolicySnippet(
        id="returns_refunds",
        title="Returns and refund policy",
        summary=(
            "Merchants must clearly state a concrete return window (e.g. a specific number of days), "
            "the return process (how to initiate a return, who pays return shipping), and refund method/timing. "
            "Vague statements like 'we accept returns' with no timeframe or process do not meet this requirement."
        ),
    ),
    PolicySnippet(
        id="shipping_policy",
        title="Shipping policy",
        summary=(
            "Merchants must disclose shipping costs, delivery timeframes (or a reasonable estimate), and the "
            "shipping regions/countries served. A shipping page that only says 'we ship worldwide' with no cost "
            "or timeframe information does not meet this requirement."
        ),
    ),
    PolicySnippet(
        id="privacy_policy",
        title="Privacy policy",
        summary=(
            "Merchants must disclose what personal data is collected, how it is used, whether it is shared with "
            "third parties, and how a customer can contact the business about their data. A privacy policy that is "
            "a generic template with no reference to the store's actual data practices is insufficient."
        ),
    ),
    PolicySnippet(
        id="terms_of_service",
        title="Terms of service",
        summary=(
            "Terms of service must describe the terms of the transaction: acceptable use, payment terms, "
            "limitation of liability, and dispute resolution. Boilerplate terms that don't reference the store's "
            "actual products or transaction type are a risk signal."
        ),
    ),
    PolicySnippet(
        id="business_identity",
        title="Business identity and contact information",
        summary=(
            "Merchants must clearly and accurately display their business name, physical address, and contact "
            "information (phone or email). This information must be consistent across all pages it appears on and "
            "must plausibly match the business's claimed country of operation."
        ),
    ),
    PolicySnippet(
        id="misrepresentation",
        title="Misrepresentation / unreliable claims",
        summary=(
            "Merchants must not make false or unsubstantiated claims about their products, business identity, or "
            "policies (e.g. fake urgency/scarcity claims, fabricated reviews, unverifiable certifications, "
            "guarantees that are not honored)."
        ),
    ),
    PolicySnippet(
        id="prohibited_content",
        title="Prohibited or restricted products",
        summary=(
            "Certain product categories are prohibited or restricted on Google Merchant Center (e.g. counterfeit "
            "goods, weapons, tobacco, recreational drugs, adult content, dangerous products, products making "
            "unsubstantiated health/medical claims). Text-based product descriptions must be screened for language "
            "suggesting these categories."
        ),
    ),
    PolicySnippet(
        id="editorial_quality",
        title="Editorial and professional content quality",
        summary=(
            "Storefront content (homepage, product pages) should be free of significant spelling/grammar errors, "
            "broken/placeholder text (e.g. 'Lorem ipsum'), and should read as a professionally operated business, "
            "not a low-effort or clearly auto-generated storefront."
        ),
    ),
]


def format_policy_snippets_for_prompt() -> str:
    return "\n\n".join(f"[{s.id}] {s.title}\n{s.summary}" for s in POLICY_SNIPPETS)


def get_snippet(policy_id: str) -> PolicySnippet | None:
    return next((s for s in POLICY_SNIPPETS if s.id == policy_id), None)
