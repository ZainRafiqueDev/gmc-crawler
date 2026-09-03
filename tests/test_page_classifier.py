"""app.page_classifier.classify_page - URL rules take priority over the
text-based fallback. Regression coverage for a real bug found live:
WooCommerce's default /my-account login/register page was misclassified as
the store's privacy policy page, because its body text includes a stock
consent notice mentioning "privacy policy" (a link to the real page, not
the page itself)."""
from app.models import PageType
from app.page_classifier import classify_page

# The exact boilerplate WooCommerce renders on its default account page -
# verified live against a real store (britanniagifts.us).
_WOOCOMMERCE_ACCOUNT_PAGE_TEXT = (
    "Your personal data will be used to support your experience throughout this website, "
    "to manage access to your account, and for other purposes described in our privacy policy."
)


def test_my_account_page_is_not_misclassified_as_privacy_policy():
    result = classify_page(
        "https://shop.example/my-account", "/my-account", is_homepage=False,
        headings=["My account"], body_text_sample=_WOOCOMMERCE_ACCOUNT_PAGE_TEXT,
    )
    assert result != PageType.PRIVACY_POLICY
    assert result == PageType.BLOG_OTHER


def test_login_and_register_urls_are_not_misclassified():
    for path in ["/login", "/register", "/my-account/lost-password"]:
        result = classify_page(
            f"https://shop.example{path}", path, is_homepage=False,
            headings=[], body_text_sample=_WOOCOMMERCE_ACCOUNT_PAGE_TEXT,
        )
        assert result == PageType.BLOG_OTHER, path


def test_real_privacy_policy_page_is_still_classified_correctly():
    result = classify_page(
        "https://shop.example/privacy-policy", "/privacy-policy", is_homepage=False,
        headings=["Privacy Policy"], body_text_sample="This privacy policy describes how we collect personal data we collect.",
    )
    assert result == PageType.PRIVACY_POLICY


def test_legal_notice_page_referencing_privacy_policy_is_not_misclassified_as_one():
    """Follow-up round: same bug class as the /my-account fix above - a
    page with its own distinct heading ("Legal Notice") that merely
    references another policy in passing ("please read our privacy
    policy") was being classified AS that policy via the body-text
    fallback. Verified live against a real store (vellano.site)."""
    result = classify_page(
        "https://shop.example/legal-notice/", "/legal-notice/", is_homepage=False,
        headings=["Legal Notice", "Regulatory Information"],
        body_text_sample=(
            "This document outlines the statutory, regulatory, and legal frameworks under which our "
            "business operates. For full details on data handling, please read our privacy policy. "
            "Federal Trade Commission (FTC) guidelines: our pricing, advertising..."
        ),
    )
    assert result == PageType.BLOG_OTHER


def test_payment_policy_page_mentioning_billing_details_is_not_misclassified_as_checkout():
    result = classify_page(
        "https://shop.example/payment-policy/", "/payment-policy/", is_homepage=False,
        headings=["Payment Policy", "Customer Transparency"],
        body_text_sample=(
            "At Vellano, we are committed to providing a secure, smooth, and reliable payment "
            "experience. This policy outlines the payment methods we accept, how transactions are "
            "processed, and important billing details. By placing an order on our website, you agree "
            "to the terms outlined here."
        ),
    )
    assert result == PageType.BLOG_OTHER


def test_shipment_policy_url_and_heading_classified_as_shipping_policy():
    """Found live against a real store (vellano.site): its own theme labels
    the shipping policy page "Shipment Policy" (URL /shipment-policy and
    matching H1) - a real, existing page that was falling through to
    BLOG_OTHER and producing a false "Missing required page: Shipping
    policy" finding despite the page being crawled successfully."""
    result = classify_page(
        "https://vellano.site/shipment-policy", "/shipment-policy", is_homepage=False,
        headings=["Shipment Policy"], body_text_sample="",
    )
    assert result == PageType.SHIPPING_POLICY


def test_body_text_fallback_still_used_when_there_is_no_heading_at_all():
    """The new guard only kicks in when a distinct heading exists - a page
    with no heading at all still gets the body-text fallback, since that's
    the best signal available in that case."""
    result = classify_page(
        "https://shop.example/policies/misc", "/policies/misc", is_homepage=False,
        headings=[], body_text_sample="Our shipping policy details delivery times and shipping costs for every order.",
    )
    assert result == PageType.SHIPPING_POLICY
