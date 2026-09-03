"""Business identity consistency check: extract address/phone/email
wherever they appear (footer, contact page, policy pages) and flag
inconsistencies - e.g. two different support emails, or a phone country
code that doesn't match the country named in the stated address.

Deliberately regex/heuristic-based for Phase 1 (no paid phone/address
parsing API) - every flagged inconsistency carries the exact evidence
strings and source URLs so a human can confirm or dismiss it.
"""
from __future__ import annotations

import re

from app.fetch import FAILURE_CATEGORY_LABELS, FAILURE_CATEGORY_RECOMMENDATIONS
from app.models import Confidence, Finding, PageType, Severity, SiteMap

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Loose international phone matcher: optional +CC, then groups of digits/
# separators, at least 8 digits total so it doesn't snag order numbers etc.
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")

# Calling code -> plausible country name fragments to cross-check against
# address text. Not exhaustive - covers the common storefront markets.
_CALLING_CODE_COUNTRIES: dict[str, list[str]] = {
    "1": ["united states", "usa", "u.s.a", "canada"],
    "44": ["united kingdom", "uk", "england", "scotland", "wales", "britain"],
    "61": ["australia"],
    "64": ["new zealand"],
    "91": ["india"],
    "49": ["germany"],
    "33": ["france"],
    "34": ["spain"],
    "39": ["italy"],
    "31": ["netherlands"],
    "353": ["ireland"],
    "27": ["south africa"],
    "65": ["singapore"],
    "971": ["united arab emirates", "uae", "dubai"],
    "92": ["pakistan"],
}

_IDENTITY_PAGE_TYPES = {
    PageType.HOMEPAGE, PageType.CONTACT_ABOUT, PageType.PRIVACY_POLICY,
    PageType.SHIPPING_POLICY, PageType.RETURNS_POLICY, PageType.TERMS_OF_SERVICE,
}

_ADDRESS_HINT_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.,'\s]{3,60}\b(street|st\.|avenue|ave\.|road|rd\.|boulevard|blvd\.|drive|dr\.|lane|ln\.|suite|floor|way|court|ct\.)\b",
    re.IGNORECASE,
)


def _normalize_phone_digits(raw: str) -> str:
    return re.sub(r"[^\d+]", "", raw)


def _guess_calling_code(digits: str) -> str | None:
    if not digits.startswith("+"):
        return None
    body = digits[1:]
    for length in (3, 2, 1):
        candidate = body[:length]
        if candidate in _CALLING_CODE_COUNTRIES:
            return candidate
    return None


def _extract_signals(text: str) -> tuple[set[str], set[str], list[str]]:
    emails = set(m.lower() for m in _EMAIL_RE.findall(text))

    phones = set()
    for m in _PHONE_RE.findall(text):
        digits = _normalize_phone_digits(m)
        digit_count = len(re.sub(r"\D", "", digits))
        if 8 <= digit_count <= 15:
            phones.add(digits)

    addresses = [m.group(0).strip() for m in _ADDRESS_HINT_RE.finditer(text)]

    return emails, phones, addresses


def check_business_identity_consistency(site_map: SiteMap) -> list[Finding]:
    # Nothing could be fetched at all - "no business contact identity found"
    # would be a confident negative from zero real information (same
    # principle as app.checks.deterministic.check_required_pages's guard;
    # see that module's docstring for the real crawl-failure bug this
    # mirrors). Worded distinctly from that check's own crawl_incomplete
    # finding (same underlying cause, different check) so the report shows
    # one clear statement per check rather than a verbatim duplicate.
    if site_map.crawl_totally_failed:
        homepage = site_map.pages[0] if site_map.pages else None
        category = homepage.failure_category if homepage else "unknown"
        label = FAILURE_CATEGORY_LABELS.get(category or "unknown", FAILURE_CATEGORY_LABELS["unknown"])
        return [Finding(
            check_id="business_identity_crawl_incomplete",
            title="Business contact identity could not be checked - this audit could not crawl the site",
            severity=Severity.HIGH,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=homepage.url if homepage else site_map.base_url,
            evidence=(
                f"No page could be fetched to check for business contact identity (email/phone/address) - {label}. "
                "This is not a confirmed absence of contact information."
            ),
            policy_reference="GMC: Store must clearly display accurate business contact information (unable to confirm - crawl did not complete)",
            recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS.get(category or "unknown", FAILURE_CATEGORY_RECOMMENDATIONS["unknown"]),
            location=None,
        )]

    findings: list[Finding] = []

    per_page: dict[str, tuple[set[str], set[str], list[str]]] = {}
    for page in site_map.pages:
        if not page.reachable or page.page_type not in _IDENTITY_PAGE_TYPES or not page.text:
            continue
        per_page[page.url] = _extract_signals(page.text)

    all_emails: dict[str, list[str]] = {}
    all_phones: dict[str, list[str]] = {}
    all_addresses: dict[str, list[str]] = {}
    for url, (emails, phones, addresses) in per_page.items():
        for e in emails:
            all_emails.setdefault(e, []).append(url)
        for p in phones:
            all_phones.setdefault(p, []).append(url)
        for a in addresses:
            all_addresses.setdefault(a, []).append(url)

    if not any([all_emails, all_phones, all_addresses]):
        findings.append(Finding(
            check_id="business_identity_present",
            title="No business contact identity found",
            severity=Severity.CRITICAL,
            confidence=Confidence.CONFIRMED,
            evidence=f"No email, phone number, or address pattern found across {len(per_page)} checked page(s) (homepage/contact/policy pages).",
            policy_reference="GMC: Store must clearly display accurate business contact information",
            recommended_fix="Add a visible business address, phone number, and support email (footer and/or contact page).",
            location="site-wide (homepage/contact/policy pages checked)",
        ))
        return findings

    if len(all_emails) > 1:
        detail = "; ".join(f"{email} (on {', '.join(urls)})" for email, urls in all_emails.items())
        findings.append(Finding(
            check_id="business_identity_email_consistency",
            title="Multiple different contact emails found across the site",
            severity=Severity.MEDIUM,
            confidence=Confidence.POTENTIAL_RISK,
            evidence=f"Found {len(all_emails)} distinct email addresses: {detail}",
            policy_reference="GMC: Business contact information must be consistent and accurate",
            recommended_fix="Confirm whether these are intentionally different (e.g. sales vs support) or a stale/incorrect address that should be unified.",
            location=f"aggregated across {len(all_emails)} pages (see evidence for exact pages/emails)",
        ))

    if len(all_phones) > 1:
        detail = "; ".join(f"{phone} (on {', '.join(urls)})" for phone, urls in all_phones.items())
        findings.append(Finding(
            check_id="business_identity_phone_consistency",
            title="Multiple different phone numbers found across the site",
            severity=Severity.MEDIUM,
            confidence=Confidence.POTENTIAL_RISK,
            evidence=f"Found {len(all_phones)} distinct phone numbers: {detail}",
            policy_reference="GMC: Business contact information must be consistent and accurate",
            recommended_fix="Confirm whether these are intentionally different (e.g. regional support lines) or inconsistent/outdated.",
            location=f"aggregated across {len(all_phones)} pages (see evidence for exact pages/numbers)",
        ))

    full_text_blob = " ".join(page.text.lower() for page in site_map.pages if page.reachable and page.text and page.page_type in _IDENTITY_PAGE_TYPES)

    for phone, urls in all_phones.items():
        code = _guess_calling_code(phone)
        if not code:
            continue
        expected_countries = _CALLING_CODE_COUNTRIES[code]
        if any(country in full_text_blob for country in expected_countries):
            continue
        # Only flag if the site does mention *some* recognizable country name
        # that conflicts - otherwise we simply don't have enough text to judge.
        mentioned = [
            country
            for countries in _CALLING_CODE_COUNTRIES.values()
            for country in countries
            if country in full_text_blob
        ]
        if mentioned:
            findings.append(Finding(
                check_id="business_identity_phone_country_mismatch",
                title="Phone country code does not match country mentioned in address/content",
                severity=Severity.HIGH,
                confidence=Confidence.POTENTIAL_RISK,
                page_url=urls[0],
                evidence=f"Phone number {phone} has calling code +{code} ({'/'.join(expected_countries)}) but the site's address/content text mentions: {', '.join(sorted(set(mentioned)))}",
                policy_reference="GMC: Business identity (name, address, contact) must be accurate and consistent with claimed location",
                recommended_fix="Confirm the store's real operating country and correct either the phone number or the stated address.",
                location=f'text containing "{phone}"',
            ))

    return findings
