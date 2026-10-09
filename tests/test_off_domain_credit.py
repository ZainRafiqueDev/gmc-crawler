"""Tests for app.off_domain_credit - detects (never fetches) returns/contact
info hosted off the audited domain, from links already collected during the
normal crawl (CrawledPage.external_links)."""
from __future__ import annotations

from app.models import CrawledPage, PageType, SiteMap
from app.off_domain_credit import find_contact_credit, find_returns_credit


def _site_map_with_links(*external_links: str, base_url: str = "https://www.gymshark.com", reachable: bool = True) -> SiteMap:
    page = CrawledPage(
        url=base_url, page_type=PageType.HOMEPAGE, depth=0, text="hi", status=200,
        reachable=reachable, external_links=list(external_links),
    )
    return SiteMap(base_url=base_url, pages=[page])


def test_returns_credited_via_same_organization_subdomain_with_returns_keyword():
    """Real gymshark.com shape: support.gymshark.com/en-US/article/returns-policy."""
    site_map = _site_map_with_links("https://support.gymshark.com/en-US/article/returns-policy")
    credit = find_returns_credit(site_map)
    assert credit is not None
    assert credit["matched_via"] == "same_organization_subdomain"
    assert credit["credited_url"] == "https://support.gymshark.com/en-US/article/returns-policy"


def test_returns_credited_via_known_third_party_portal():
    """Real gymshark.com shape: us-gymshark.loopreturns.com."""
    site_map = _site_map_with_links("https://us-gymshark.loopreturns.com/#/")
    credit = find_returns_credit(site_map)
    assert credit is not None
    assert credit["matched_via"] == "known_returns_portal"


def test_contact_credited_via_same_organization_subdomain_with_support_keyword():
    site_map = _site_map_with_links("https://support.gymshark.com/en-US")
    credit = find_contact_credit(site_map)
    assert credit is not None
    assert credit["matched_via"] == "same_organization_subdomain"


def test_no_credit_when_no_off_domain_link_at_all():
    site_map = _site_map_with_links()
    assert find_returns_credit(site_map) is None
    assert find_contact_credit(site_map) is None


def test_unrelated_external_domain_is_not_credited():
    site_map = _site_map_with_links("https://www.instagram.com/gymshark/", "https://www.facebook.com/gymshark")
    assert find_returns_credit(site_map) is None
    assert find_contact_credit(site_map) is None


def test_same_organization_subdomain_without_a_keyword_is_not_credited():
    """A bare same-org subdomain alone (e.g. a CDN/assets host) must not be
    credited - the keyword gate is what makes this a real signal, not just
    "this store happens to use subdomains."""
    site_map = _site_map_with_links("https://cdn.gymshark.com/assets/logo.png")
    assert find_returns_credit(site_map) is None
    assert find_contact_credit(site_map) is None


def test_known_returns_portal_does_not_also_credit_contact():
    """A returns-portal domain is a returns signal specifically, not a
    general contact/support signal."""
    site_map = _site_map_with_links("https://us-gymshark.loopreturns.com/#/")
    assert find_contact_credit(site_map) is None


def test_links_on_unreachable_pages_are_ignored():
    site_map = _site_map_with_links("https://support.gymshark.com/en-US/article/returns-policy", reachable=False)
    assert find_returns_credit(site_map) is None


def test_credit_never_fetches_anything_just_reads_already_collected_links():
    """No network/browser object is passed to either function at all - a
    structural guarantee, not just a behavioral one, that detection can
    never fetch anything new."""
    import inspect
    assert list(inspect.signature(find_returns_credit).parameters) == ["site_map"]
    assert list(inspect.signature(find_contact_credit).parameters) == ["site_map"]
