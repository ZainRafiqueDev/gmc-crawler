"""Structural form checks (app/checks/form_checks.py) - no real submission
ever happens; only GET on the declared action URL to check reachability."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.checks.form_checks import check_forms
from app.models import Confidence, CrawledPage, PageType, SiteMap
from app.security.ssrf_guard import DNSResolutionError, SSRFBlockedError


def _page(url: str, html: str, page_type: PageType = PageType.CONTACT_ABOUT) -> CrawledPage:
    return CrawledPage(url=url, page_type=page_type, depth=0, reachable=True, html=html)


def _site_map(*pages: CrawledPage) -> SiteMap:
    return SiteMap(base_url=pages[0].url if pages else "https://shop.example/", pages=list(pages))


def _mock_response(status_code: int = 200):
    resp = httpx.Response(status_code=status_code, request=httpx.Request("GET", "https://shop.example/submit"))
    return resp


@pytest.mark.asyncio
async def test_contact_form_with_all_fields_produces_no_field_finding():
    html = """
    <form id="contact-form" action="/submit-contact">
        <input type="text" name="your-name" placeholder="Name">
        <input type="email" name="your-email" placeholder="Email">
        <textarea name="message" placeholder="Message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(200))
        findings = await check_forms(_site_map(page))
    assert not any(f.check_id == "contact_form_missing_field" for f in findings)


@pytest.mark.asyncio
async def test_contact_form_missing_message_field_is_flagged():
    html = """
    <form id="contact-form" action="/submit-contact">
        <input type="text" name="your-name" placeholder="Name">
        <input type="email" name="your-email" placeholder="Email">
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(200))
        findings = await check_forms(_site_map(page))
    missing = [f for f in findings if f.check_id == "contact_form_missing_field"]
    assert len(missing) == 1
    assert "message" in missing[0].title


@pytest.mark.asyncio
async def test_form_action_returning_404_is_flagged():
    html = """
    <form id="contact-form" action="/submit-contact">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(404))
        findings = await check_forms(_site_map(page))
    unreachable = [f for f in findings if f.check_id == "form_action_unreachable"]
    assert len(unreachable) == 1
    assert "404" in unreachable[0].evidence


# --- Failure-reporting specificity (follow-up round, Part 1.3) -------------

def _form_html() -> str:
    return """
    <form id="contact-form" action="/submit-contact">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """


@pytest.mark.asyncio
async def test_dns_hiccup_on_form_action_is_cannot_verify_not_a_confirmed_block():
    """A DNSResolutionError must never be reported as a confirmed
    non-public-address block (Confidence.CONFIRMED) - that's the exact
    false-positive class already found and fixed once for page fetches
    (app/fetch.py, the britanniagifts.us live incident); this form-action
    probe is a separate code path that had the same gap."""
    page = _page("https://shop.example/contact", _form_html())
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=DNSResolutionError("Could not resolve host 'shop.example': timeout"))
        findings = await check_forms(_site_map(page))
    unreachable = [f for f in findings if f.check_id == "form_action_unreachable"]
    assert len(unreachable) == 1
    assert unreachable[0].confidence == Confidence.CANNOT_VERIFY
    assert "network error" in unreachable[0].title.lower()


@pytest.mark.asyncio
async def test_real_ssrf_block_on_form_action_stays_confirmed():
    page = _page("https://shop.example/contact", _form_html())
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=SSRFBlockedError("Host 'internal.local' resolves to a blocked address: 10.0.0.5"))
        findings = await check_forms(_site_map(page))
    unreachable = [f for f in findings if f.check_id == "form_action_unreachable"]
    assert len(unreachable) == 1
    assert unreachable[0].confidence == Confidence.CONFIRMED
    assert "blocked/non-public address" in unreachable[0].title


@pytest.mark.asyncio
async def test_generic_network_timeout_names_a_specific_category():
    page = _page("https://shop.example/contact", _form_html())
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        findings = await check_forms(_site_map(page))
    unreachable = [f for f in findings if f.check_id == "form_action_unreachable"]
    assert len(unreachable) == 1
    assert unreachable[0].confidence == Confidence.CANNOT_VERIFY
    assert "network error" in unreachable[0].title.lower()
    assert "DNS/connectivity" in unreachable[0].recommended_fix


@pytest.mark.asyncio
async def test_no_real_submission_is_ever_made_only_get():
    html = """
    <form id="contact-form" action="/submit-contact" method="post">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_get = AsyncMock(return_value=_mock_response(200))
        mock_client.return_value.__aenter__.return_value.get = mock_get
        await check_forms(_site_map(page))
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "https://shop.example/submit-contact"


@pytest.mark.asyncio
async def test_newsletter_form_email_only_is_not_flagged_for_missing_name_or_message():
    html = """
    <form id="newsletter-signup" action="/subscribe">
        <input type="email" name="email" placeholder="Your email">
        <button type="submit">Subscribe</button>
    </form>
    """
    page = _page("https://shop.example/", html, page_type=PageType.HOMEPAGE)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(200))
        findings = await check_forms(_site_map(page))
    assert not any(f.check_id == "contact_form_missing_field" for f in findings)


@pytest.mark.asyncio
async def test_search_form_is_ignored_entirely():
    html = """
    <form id="search-form" action="/search" method="get">
        <input type="search" name="q">
    </form>
    """
    page = _page("https://shop.example/", html, page_type=PageType.HOMEPAGE)
    findings = await check_forms(_site_map(page))
    assert findings == []


@pytest.mark.asyncio
async def test_mailto_action_is_not_probed_over_network():
    html = """
    <form id="contact-form" action="mailto:owner@shop.example">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        findings = await check_forms(_site_map(page))
        mock_client.assert_not_called()
    assert not any(f.check_id == "form_action_unreachable" for f in findings)


@pytest.mark.asyncio
async def test_email_field_without_type_or_required_flags_weak_validation():
    html = """
    <form id="contact-form" action="/submit">
        <input type="text" name="your-name">
        <input type="text" name="your-email" placeholder="Email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(200))
        findings = await check_forms(_site_map(page))
    assert any(f.check_id == "form_email_field_weak_validation" for f in findings)


@pytest.mark.asyncio
async def test_same_page_anchor_action_is_not_probed_over_network():
    """Regression: Contact Form 7 (and similar) sets action="/current-page/
    #wpcf7-fN-oM" - a client-side scroll anchor its own JS uses after an
    AJAX submit, not a distinct endpoint. Verified live: probing it produced
    a false-positive 403 (bot-protection blocking a plain httpx GET) on a
    page Playwright had just loaded successfully seconds earlier."""
    html = """
    <form id="contact-form" action="/contact/#wpcf7-f210-o1" method="post">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        findings = await check_forms(_site_map(page))
        mock_client.assert_not_called()
    assert not any(f.check_id == "form_action_unreachable" for f in findings)


@pytest.mark.asyncio
async def test_login_form_with_password_field_is_not_a_lead_gen_form():
    """Regression: WooCommerce's login form labels its username field
    "Username or email", which superficially looks like an email field on a
    <=2-field form (the same shape as a real newsletter signup) - verified
    live against a real WooCommerce login form. A password field is an
    unambiguous signal this is an account form, not lead-gen."""
    html = """
    <form method="post" class="woocommerce-form woocommerce-form-login login">
        <input type="text" name="username" id="u"><label for="u">Username or email</label>
        <input type="password" name="password">
    </form>
    """
    page = _page("https://shop.example/my-account", html, page_type=PageType.BLOG_OTHER)
    findings = await check_forms(_site_map(page))
    assert findings == []


@pytest.mark.asyncio
async def test_register_form_matched_via_non_first_css_class_token_is_excluded():
    """Regression: form.get("class") returns every class token as a list;
    WooCommerce's register form is class="woocommerce-form
    woocommerce-form-register register" - the identifying token isn't
    first, so matching only class[0] misses it - verified live."""
    html = """
    <form method="post" class="woocommerce-form woocommerce-form-register register">
        <input type="email" name="email" required>
    </form>
    """
    page = _page("https://shop.example/my-account", html, page_type=PageType.BLOG_OTHER)
    findings = await check_forms(_site_map(page))
    assert findings == []


@pytest.mark.asyncio
async def test_woocommerce_review_form_is_not_a_lead_gen_form():
    """Regression: WordPress core's native product-review/comment form
    (rendered by any WooCommerce store with reviews enabled - not a
    per-theme customization) was misdetected as a broken contact form on
    every product page - verified live against a real store (vellano.site,
    markup below is the exact HTML that store served). Two compounding
    false positives: (1) the review form's "Name" field has name/id="author",
    which doesn't literally contain "name" and whose <label for="author">
    is a *sibling*, not a parent, of the input, so _field_hint never sees
    "Name" either - the generic contact-form check therefore always reports
    it as missing a name field, on every product page, for every
    WooCommerce store with reviews enabled; (2) its action
    (wp-comments-post.php) legitimately rejects a plain GET with 403 -
    that's WordPress core's own POST-only design, not a broken endpoint,
    but the form-reachability check has no way to know that once it's
    wrongly treated as a contact form worth probing. This produced 10 false
    "suspension risk" findings across 5 product pages in one real report -
    fully suppressed by treating this as excluded like the login/register
    forms, not by trying to fix each individual downstream symptom."""
    html = """
    <form id="commentform" class="comment-form" method="post" action="https://vellano.site/wp-comments-post.php">
        <p class="comment-form-rating"><label for="rating">Your rating</label>
            <select name="rating" id="rating"><option value="5">Perfect</option></select></p>
        <p class="comment-form-comment"><label for="comment">Your review</label>
            <textarea id="comment" name="comment" cols="45" rows="8"></textarea></p>
        <p class="comment-form-author"><label for="author">Name</label>
            <input id="author" name="author" type="text" size="30"></p>
        <p class="comment-form-email"><label for="email">Email</label>
            <input id="email" name="email" type="email" size="30"></p>
        <p class="form-submit"><input name="submit" type="submit" id="submit" class="submit" value="Submit">
            <input name="comment_post_ID" value="231" id="comment_post_ID" type="hidden">
            <input name="comment_parent" id="comment_parent" type="hidden" value="0"></p>
    </form>
    """
    page = _page("https://vellano.site/product/some-product/", html, page_type=PageType.PRODUCT)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=AssertionError("must not probe a review form's action URL at all"))
        findings = await check_forms(_site_map(page))
    assert findings == []


@pytest.mark.asyncio
async def test_newsletter_widget_on_contact_page_is_not_flagged_as_broken_contact_form():
    """Regression: a sitewide footer newsletter widget (single email field,
    "SUBSCRIBE" submit button) rendering on the contact page was
    misclassified as a contact form missing name/message fields, purely
    because of the page's PageType - verified live against a real store."""
    html = """
    <form action="/contact-us/#wpcf7-f210-o1" method="post" aria-label="Contact form">
        <input type="email" name="your-email" placeholder="Enter your email here">
        <input type="submit" value="SUBSCRIBE" class="btn-submit-newsletters">
    </form>
    """
    page = _page("https://shop.example/contact-us", html, page_type=PageType.CONTACT_ABOUT)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        findings = await check_forms(_site_map(page))
        mock_client.assert_not_called()  # same-page anchor - never probed either
    assert not any(f.check_id == "contact_form_missing_field" for f in findings)


@pytest.mark.asyncio
async def test_email_type_field_does_not_flag_weak_validation():
    html = """
    <form id="contact-form" action="/submit">
        <input type="text" name="your-name">
        <input type="email" name="your-email">
        <textarea name="message"></textarea>
    </form>
    """
    page = _page("https://shop.example/contact", html)
    with patch("app.checks.form_checks.safe_async_client") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(200))
        findings = await check_forms(_site_map(page))
    assert not any(f.check_id == "form_email_field_weak_validation" for f in findings)
