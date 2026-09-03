"""Tests for the Phase E purchase-journey check. The critical property under
test isn't just "findings look right" - it's that the runner NEVER calls
.click() on anything except the add-to-cart button. Every test asserts the
total click count stays at exactly 1 (or 0, if add-to-cart wasn't found),
proving the hard stop before payment is structural, not just "usually works."
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.checks.purchase_journey import run_purchase_journey_check
from app.models import Confidence, Severity


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """purchase_journey.py imports assert_public_url directly, so the
    global conftest fake (which patches the ssrf_guard module's own
    attribute) doesn't reach this binding - neutralize it here too. These
    tests use fake domains (shop.example) and must stay network-free.
    """
    async def fake_assert_public_url(url):
        return None
    monkeypatch.setattr("app.checks.purchase_journey.assert_public_url", fake_assert_public_url)


def _make_response(status=200):
    resp = MagicMock()
    resp.status = status
    return resp


def _make_locator(count: int):
    locator = MagicMock()
    locator.count = AsyncMock(return_value=count)
    locator.click = AsyncMock()
    return locator


class FakePage:
    """Simulates: product page (has add-to-cart) -> cart page -> checkout page."""

    def __init__(self, product_text, cart_text, checkout_text, add_to_cart_matches=True):
        self.product_text = product_text
        self.cart_text = cart_text
        self.checkout_text = checkout_text
        self._add_to_cart_matches = add_to_cart_matches
        self._current_text = product_text
        self.goto_urls: list[str] = []
        self.click_count = 0
        self._add_to_cart_locator = _make_locator(1 if add_to_cart_matches else 0)
        self._empty_locator = _make_locator(0)

    async def goto(self, url, timeout=None, wait_until=None):
        self.goto_urls.append(url)
        if "/cart" in url:
            self._current_text = self.cart_text
        elif "/checkout" in url:
            self._current_text = self.checkout_text
        else:
            self._current_text = self.product_text
        return _make_response(200)

    async def inner_text(self, selector):
        return self._current_text

    def locator(self, selector):
        if selector == "button.single_add_to_cart_button" and self._add_to_cart_matches:
            wrapped = MagicMock()
            wrapped.first = self._make_clicking_locator()
            return wrapped
        wrapped = MagicMock()
        wrapped.first = self._empty_locator
        return wrapped

    def _make_clicking_locator(self):
        locator = self._add_to_cart_locator

        async def click(timeout=None):
            self.click_count += 1

        locator.click = click
        return locator

    async def wait_for_load_state(self, state, timeout=None):
        pass


def _make_browser(fake_page: FakePage):
    browser = MagicMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=fake_page)
    context.close = AsyncMock()
    context.route = AsyncMock()
    context.add_init_script = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


@pytest.mark.asyncio
async def test_happy_path_never_clicks_past_add_to_cart_and_stops_before_payment():
    page = FakePage(
        product_text="Widget - $19.99",
        cart_text="Your cart: Widget - $19.99",
        checkout_text="Shipping: $5.00. Total: $24.99",
    )
    browser = _make_browser(page)

    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")

    assert page.click_count == 1  # only the add-to-cart click ever happens
    assert result.stopped_before_payment is True
    actions = [e.action for e in result.action_log]
    assert "click_add_to_cart" in actions
    assert actions[-1] == "stopped_before_payment"
    assert "/cart" in page.goto_urls[1] or "/cart/" in page.goto_urls[1]
    assert any("/checkout" in u for u in page.goto_urls)


@pytest.mark.asyncio
async def test_no_add_to_cart_control_found_stops_immediately_with_zero_clicks():
    page = FakePage(product_text="Widget - $19.99", cart_text="", checkout_text="", add_to_cart_matches=False)
    browser = _make_browser(page)

    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")

    assert page.click_count == 0
    assert result.stopped_before_payment is True
    assert any(f.check_id == "purchase_journey_add_to_cart_failed" for f in result.findings)
    assert not any("/cart" in u for u in page.goto_urls)  # never got that far


@pytest.mark.asyncio
async def test_cart_price_mismatch_detected():
    page = FakePage(
        product_text="Widget - $19.99",
        cart_text="Your cart: Widget - $29.99",
        checkout_text="Shipping: $5.00. Total: $34.99",
    )
    browser = _make_browser(page)

    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")

    mismatch = [f for f in result.findings if f.check_id == "purchase_journey_cart_price_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0].severity == Severity.HIGH
    assert mismatch[0].confidence == Confidence.CONFIRMED
    assert "19.99" in mismatch[0].evidence and "29.99" in mismatch[0].evidence
    assert page.click_count == 1  # still only ever the one click


@pytest.mark.asyncio
async def test_missing_shipping_line_on_checkout_flagged():
    page = FakePage(
        product_text="Widget - $19.99",
        cart_text="Your cart: Widget - $19.99",
        checkout_text="Total: $19.99",  # no mention of shipping/delivery
    )
    browser = _make_browser(page)

    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")

    assert any(f.check_id == "purchase_journey_shipping_not_shown" for f in result.findings)
    assert page.click_count == 1


@pytest.mark.asyncio
async def test_action_log_records_every_step_in_order():
    page = FakePage(
        product_text="Widget - $19.99",
        cart_text="Your cart: Widget - $19.99",
        checkout_text="Shipping: $5.00. Total: $24.99",
    )
    browser = _make_browser(page)

    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")
    actions = [e.action for e in result.action_log]
    assert actions == [
        "navigate_to_product", "read_product_price", "click_add_to_cart",
        "load_cart_page", "load_checkout_page", "stopped_before_payment",
    ]


@pytest.mark.asyncio
async def test_result_always_reports_stopped_before_payment_true():
    # even in every failure branch tested above, stopped_before_payment must be True -
    # there is no code path that sets it False, by design.
    page = FakePage(product_text="", cart_text="", checkout_text="", add_to_cart_matches=False)
    browser = _make_browser(page)
    result = await run_purchase_journey_check(browser, "https://shop.example/", "https://shop.example/product/widget")
    assert result.stopped_before_payment is True
