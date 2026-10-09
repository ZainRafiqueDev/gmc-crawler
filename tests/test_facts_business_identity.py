"""Tests for app.facts.business_identity_facts - generalizes
app.checks.business_identity's existing regexes into canonical facts."""
from __future__ import annotations

from app.facts.business_identity_facts import extract_business_identity_facts
from app.facts.keys import (
    BUSINESS_ADDRESS_COUNTRY,
    BUSINESS_EMAIL,
    BUSINESS_LEGAL_ENTITY,
    BUSINESS_NAME,
    BUSINESS_PHONE,
)
from app.models import CrawledPage, PageType, SiteMap


def _facts_of(facts, key):
    return [f for f in facts if f.fact == key]


def test_email_and_phone_reuse_existing_business_identity_patterns():
    page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="Contact us at support@example.com or call +1 415 555 0100.",
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)

    emails = _facts_of(facts, BUSINESS_EMAIL)
    assert len(emails) == 1 and emails[0].value == "support@example.com"
    assert emails[0].method == "business_identity_regex"

    phones = _facts_of(facts, BUSINESS_PHONE)
    assert len(phones) == 1 and phones[0].value == "+14155550100"


def test_address_country_from_calling_code_cross_reference():
    page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="Call us: +44 20 7946 0958",
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)
    countries = _facts_of(facts, BUSINESS_ADDRESS_COUNTRY)
    assert any(f.value == "GB" and f.method == "calling_code_cross_reference" for f in countries)


def test_address_country_from_text_mention():
    page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="Our office is located in Germany.",
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)
    countries = _facts_of(facts, BUSINESS_ADDRESS_COUNTRY)
    assert any(f.value == "DE" and f.method == "address_text_country_regex" for f in countries)


def test_legal_entity_and_name_from_copyright_footer():
    page = CrawledPage(
        url="https://example.com", page_type=PageType.HOMEPAGE, depth=0,
        text="Welcome to our store. © 2025 Acme Widgets Inc. All rights reserved.",
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)
    legal = _facts_of(facts, BUSINESS_LEGAL_ENTITY)
    names = _facts_of(facts, BUSINESS_NAME)

    assert any("Acme Widgets Inc" in f.value for f in legal)
    assert any(f.value == "Acme Widgets" for f in names)


def test_legal_entity_from_operated_by_sentence():
    page = CrawledPage(
        url="https://example.com/terms", page_type=PageType.TERMS_OF_SERVICE, depth=1,
        text="This website is operated by Northwind Traders LLC.",
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)
    legal = _facts_of(facts, BUSINESS_LEGAL_ENTITY)
    assert any("Northwind Traders LLC" in f.value and f.method == "legal_suffix_regex" for f in legal)


def test_heading_heuristic_used_on_contact_page_not_policy_page():
    contact_page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="Reach out any time.", headings=["Acme Widgets"],
    )
    privacy_page = CrawledPage(
        url="https://example.com/privacy", page_type=PageType.PRIVACY_POLICY, depth=1,
        text="We respect your privacy.", headings=["Privacy Policy"],
    )
    site_map = SiteMap(base_url="https://example.com", pages=[contact_page, privacy_page])

    facts = extract_business_identity_facts(site_map)
    names = _facts_of(facts, BUSINESS_NAME)

    assert any(f.value == "Acme Widgets" and f.method == "heading_heuristic" for f in names)
    assert not any(f.value == "Privacy Policy" for f in names)  # generic policy-page heading never used


def test_generic_heading_is_not_mistaken_for_a_business_name():
    page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="Reach out any time.", headings=["Contact Us"],
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])

    facts = extract_business_identity_facts(site_map)
    names = _facts_of(facts, BUSINESS_NAME)
    assert names == []


def test_unreachable_page_contributes_no_facts():
    page = CrawledPage(
        url="https://example.com/contact", page_type=PageType.CONTACT_ABOUT, depth=1,
        text="support@example.com", reachable=False,
    )
    site_map = SiteMap(base_url="https://example.com", pages=[page])
    assert extract_business_identity_facts(site_map) == []
