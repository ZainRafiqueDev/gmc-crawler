"""Shared test fixtures - a small constructed "sample store" used to prove
the external-domain-link and business-identity-consistency checks without
depending on a live target site.
"""
from __future__ import annotations

import pytest

from app.models import CrawledPage, PageType, SiteMap


@pytest.fixture(autouse=True)
def _no_real_dns_for_ssrf_guard(monkeypatch):
    """Global default: the SSRF guard (app/security/ssrf_guard.py) resolves
    DNS for real before delegating to httpx/Playwright, but most tests in
    this suite use fake domains (x.example, shop.example.com) via respx or
    mocked browsers and must stay network-free. Dedicated SSRF tests
    (tests/test_ssrf_guard.py, and the SSRF-specific cases in
    tests/test_fetch.py) explicitly re-monkeypatch this within their own
    test to exercise the real logic or simulate a block.
    """
    async def fake_assert_public_url(url):
        return None
    monkeypatch.setattr("app.security.ssrf_guard.assert_public_url", fake_assert_public_url)

HOMEPAGE_HTML = """
<html><head><title>Acme Gadgets</title></head>
<body>
<header><nav><a href="/">Home</a><a href="/shop">Shop</a><a href="/contact-us">Contact</a></nav></header>
<main>
  <h1>Acme Gadgets</h1>
  <p>Follow us on <a href="https://facebook.com/acmegadgets">Facebook</a> and
  <a href="https://instagram.com/acmegadgets">Instagram</a>.</p>
  <a href="https://totally-unrelated-blog.example/some-article">Check out this article</a>
</main>
<footer>
  <p>Acme Gadgets Ltd, 221B Baker Street, London, United Kingdom</p>
  <p>Email: support@acmegadgets.co.uk | Phone: +44 20 7946 0958</p>
  <a href="/privacy-policy">Privacy Policy</a>
  <a href="/shipping-policy">Shipping Policy</a>
  <a href="/returns-policy">Returns Policy</a>
  <a href="/terms-of-service">Terms of Service</a>
</footer>
</body></html>
"""

CONTACT_HTML = """
<html><head><title>Contact Us - Acme Gadgets</title></head>
<body>
<header><nav><a href="/">Home</a><a href="/shop">Shop</a><a href="/contact-us">Contact</a></nav></header>
<main>
  <h1>Contact Us</h1>
  <p>Reach our sales team at sales@acme-us-office.com or call +1 (415) 555-0199.</p>
  <p>Our office: 500 Market Street, San Francisco, CA, USA</p>
</main>
<footer>
  <p>Acme Gadgets Ltd, 221B Baker Street, London, United Kingdom</p>
  <p>Email: support@acmegadgets.co.uk | Phone: +44 20 7946 0958</p>
</footer>
</body></html>
"""

PRIVACY_HTML = """
<html><head><title>Privacy Policy - Acme Gadgets</title></head>
<body>
<header><nav><a href="/">Home</a><a href="/shop">Shop</a><a href="/contact-us">Contact</a></nav></header>
<main>
  <h1>Privacy Policy</h1>
  <p>We collect the personal data you provide when placing an order.</p>
</main>
<footer>
  <p>Acme Gadgets Ltd, 221B Baker Street, London, United Kingdom</p>
  <p>Email: support@acmegadgets.co.uk | Phone: +44 20 7946 0958</p>
</footer>
</body></html>
"""

_FOOTER_LINKS = '<a href="/privacy-policy">Privacy Policy</a><a href="/shipping-policy">Shipping</a><a href="/terms-of-service">Terms</a><a href="/contact-us">Contact</a>'

# Two copies of the same footer is normal responsive-theme behavior (desktop
# + mobile toggle) and must NOT be flagged.
TWO_FOOTERS_HTML = f"""
<html><head><title>Shop - Acme Gadgets</title></head>
<body>
<header><nav><a href="/">Home</a><a href="/shop">Shop</a></nav></header>
<main><h1>Shop</h1><p>Buy widgets.</p></main>
<footer>{_FOOTER_LINKS}</footer>
<footer>{_FOOTER_LINKS}</footer>
</body></html>
"""

# Three+ copies of the same footer is a real templating bug and should be flagged.
THREE_FOOTERS_HTML = f"""
<html><head><title>Deals - Acme Gadgets</title></head>
<body>
<header><nav><a href="/">Home</a><a href="/shop">Shop</a></nav></header>
<main><h1>Deals</h1><p>Buy widgets on sale.</p></main>
<footer>{_FOOTER_LINKS}</footer>
<footer>{_FOOTER_LINKS}</footer>
<footer>{_FOOTER_LINKS}</footer>
</body></html>
"""


def _page(url: str, page_type: PageType, html: str, depth: int = 1, external_links: list[str] | None = None) -> CrawledPage:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    internal, external = [], list(external_links or [])
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http"):
            external.append(href)
        elif href.startswith("/"):
            internal.append(f"https://acmegadgets.example{href}")

    return CrawledPage(
        url=url,
        page_type=page_type,
        depth=depth,
        title=title,
        headings=[h.get_text(strip=True) for h in soup.find_all(["h1", "h2"])],
        status=200,
        reachable=True,
        internal_links=sorted(set(internal)),
        external_links=sorted(set(external)),
        html=html,
        text=text,
    )


@pytest.fixture
def sample_site_map() -> SiteMap:
    pages = [
        _page("https://acmegadgets.example/", PageType.HOMEPAGE, HOMEPAGE_HTML, depth=0),
        _page("https://acmegadgets.example/contact-us", PageType.CONTACT_ABOUT, CONTACT_HTML),
        _page("https://acmegadgets.example/privacy-policy", PageType.PRIVACY_POLICY, PRIVACY_HTML),
        _page("https://acmegadgets.example/shop", PageType.COLLECTION, TWO_FOOTERS_HTML),
    ]
    return SiteMap(base_url="https://acmegadgets.example/", pages=pages, sitemap_urls_found=0)
