"""Generalizes app.checks.business_identity's existing extraction into
canonical facts (business.name, business.legal_entity, business.email,
business.phone, business.address, business.address_country).

Reuses that module's own regexes/helpers directly (_EMAIL_RE, _PHONE_RE,
_normalize_phone_digits, _ADDRESS_HINT_RE, _CALLING_CODE_COUNTRIES,
_guess_calling_code, _IDENTITY_PAGE_TYPES) rather than re-typing them - if
that check's own pattern improves, this extractor benefits automatically
instead of silently drifting out of sync. business_identity.py itself is
untouched; its own check_business_identity_consistency function keeps
working exactly as it does today (and is what business_identity_conflict's
`existing_check` rule condition still delegates to - see starter_rules.yaml).

Business name/legal entity are NEW extraction (business_identity.py never
extracted these) - copyright-footer and "operated by ..." patterns, the same
regex/heuristic Phase-1 style as everything else in that module.
"""
from __future__ import annotations

import re

from app.checks.business_identity import (
    _CALLING_CODE_COUNTRIES,
    _IDENTITY_PAGE_TYPES,
    _extract_signals,
    _guess_calling_code,
)
from app.facts.keys import (
    BUSINESS_ADDRESS,
    BUSINESS_ADDRESS_COUNTRY,
    BUSINESS_EMAIL,
    BUSINESS_LEGAL_ENTITY,
    BUSINESS_NAME,
    BUSINESS_PHONE,
)
from app.facts.normalize import COUNTRY_NAME_TO_ALPHA2, find_country_mentions
from app.facts.types import FactRecord, make_fact
from app.models import PageType, SiteMap

# Heading heuristic is only trustworthy on these page types - a
# PRIVACY_POLICY/TERMS_OF_SERVICE page's first heading is almost always the
# policy's own title ("Privacy Policy"), not the business name.
_HEADING_HEURISTIC_PAGE_TYPES = {PageType.HOMEPAGE, PageType.CONTACT_ABOUT}

_LEGAL_SUFFIX = r"(?:Inc\.?|L\.?L\.?C\.?|Ltd\.?|GmbH|Pty\.?\s?Ltd\.?|Corp\.?|Corporation|Co\.?)"

_COPYRIGHT_RE = re.compile(
    rf"(?:©|\(c\)|copyright)\s*(?:\d{{4}}\s*)?(?P<name>[A-Z][\w&',.\- ]{{1,60}}?{_LEGAL_SUFFIX}?)(?=[.,\n]|\s+all rights reserved|$)",
    re.IGNORECASE,
)
_OPERATED_BY_RE = re.compile(
    rf"(?:operated by|is a service of|is owned by)\s+(?P<name>[A-Z][\w&',.\- ]{{1,60}}?{_LEGAL_SUFFIX})",
    re.IGNORECASE,
)
_SUFFIX_SPLIT_RE = re.compile(rf"^(?P<base>.+?)[,\s]+(?P<suffix>{_LEGAL_SUFFIX})\.?\s*$", re.IGNORECASE)

_GENERIC_HEADINGS = {"contact us", "about us", "about", "contact", "get in touch", "our story"}


def _calling_code_to_alpha2(code: str) -> str | None:
    for fragment in _CALLING_CODE_COUNTRIES.get(code, []):
        if fragment in COUNTRY_NAME_TO_ALPHA2:
            return COUNTRY_NAME_TO_ALPHA2[fragment]
    return None


def _name_and_legal_entity_facts(url: str, text: str) -> list[FactRecord]:
    facts: list[FactRecord] = []

    for regex, method in ((_OPERATED_BY_RE, "legal_suffix_regex"), (_COPYRIGHT_RE, "copyright_footer_regex")):
        m = regex.search(text)
        if not m:
            continue
        raw_name = " ".join(m.group("name").split())
        facts.append(make_fact(url, BUSINESS_LEGAL_ENTITY if _SUFFIX_SPLIT_RE.match(raw_name) else BUSINESS_NAME, raw_name, url, m.group(0).strip(), method))

        suffix_match = _SUFFIX_SPLIT_RE.match(raw_name)
        if suffix_match:
            facts.append(make_fact(url, BUSINESS_NAME, suffix_match.group("base").strip(), url, m.group(0).strip(), method))
        break  # first regex to match wins - don't double-emit from both patterns on the same page

    return facts


def _heading_name_fact(url: str, headings: list[str]) -> FactRecord | None:
    for heading in headings:
        cleaned = heading.strip()
        if cleaned and cleaned.lower() not in _GENERIC_HEADINGS and len(cleaned) <= 60:
            return make_fact(url, BUSINESS_NAME, cleaned, url, cleaned, "heading_heuristic")
    return None


def extract_business_identity_facts(site_map: SiteMap) -> list[FactRecord]:
    facts: list[FactRecord] = []

    for page in site_map.pages:
        if not page.reachable or page.page_type not in _IDENTITY_PAGE_TYPES or not page.text:
            continue

        emails, phones, addresses = _extract_signals(page.text)
        for email in emails:
            facts.append(make_fact(page.url, BUSINESS_EMAIL, email, page.url, email, "business_identity_regex"))
        for phone in phones:
            facts.append(make_fact(page.url, BUSINESS_PHONE, phone, page.url, phone, "business_identity_regex"))
        for address in addresses:
            facts.append(make_fact(page.url, BUSINESS_ADDRESS, address, page.url, address, "business_identity_regex"))

        for country in find_country_mentions(page.text):
            facts.append(make_fact(page.url, BUSINESS_ADDRESS_COUNTRY, country, page.url, country, "address_text_country_regex"))

        for phone in phones:
            code = _guess_calling_code(phone)
            if not code:
                continue
            alpha2 = _calling_code_to_alpha2(code)
            if alpha2:
                facts.append(make_fact(page.url, BUSINESS_ADDRESS_COUNTRY, alpha2, page.url, phone, "calling_code_cross_reference"))

        facts.extend(_name_and_legal_entity_facts(page.url, page.text))

        if page.page_type in _HEADING_HEURISTIC_PAGE_TYPES:
            heading_fact = _heading_name_fact(page.url, page.headings)
            if heading_fact is not None:
                facts.append(heading_fact)

    return facts
