"""Phase E: purchase-journey verification - add a sample product to cart,
verify it, proceed to checkout, verify displayed shipping/total, then stop.

SAFETY DESIGN (non-negotiable, read before touching this file):
  - Opt-in only. The caller (audit.py) must pass BOTH --enable-purchase-journey
    and --confirm-test-payment-mode; there is no default-on path anywhere.
  - This code NEVER clicks a submit/place-order/pay/checkout-completion
    control, full stop - not "stops before clicking it if it recognizes it."
    The flow is a fixed, small allowlist of actions (add to cart -> load the
    cart page -> load the checkout page -> read what's displayed) and ends
    there structurally. There is no code path in this module that submits a
    form on a checkout/payment page, fills in a card field, or clicks
    anything past "proceed to checkout." Extending this file to go further
    requires deliberately rewriting this docstring and the design below,
    not just adding a call.
  - Every action taken is appended to the action log returned to the
    caller, so there's a full audit trail of exactly what the bot did.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from playwright.async_api import Browser
from pydantic import BaseModel

from app.fetch import BROWSER_USER_AGENT, STEALTH_INIT_SCRIPT, STEALTH_VIEWPORT
from app.models import Confidence, Finding, Severity
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url, install_ssrf_guard

logger = logging.getLogger("gmc_audit.checks.purchase_journey")

_NAV_TIMEOUT_MS = 20_000
_PRICE_RE = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d{2})?")

# Add-to-cart selectors, most specific first. Deliberately does NOT include
# "buy now" / "quick buy" controls - those often skip the cart and jump
# straight to checkout or an accelerated payment sheet (Shop Pay, Apple Pay),
# which this module must never touch.
_ADD_TO_CART_SELECTORS = [
    "button.single_add_to_cart_button",       # WooCommerce
    "button[name='add-to-cart']",               # WooCommerce (some themes)
    "form[action*='/cart/add'] button[type='submit']",  # Shopify
    "button[name='add']",                        # Shopify (common theme pattern)
]

_CART_PATH_CANDIDATES = ["/cart/", "/cart"]
_CHECKOUT_PATH_CANDIDATES = ["/checkout/", "/checkout"]


class ActionLogEntry(BaseModel):
    timestamp: str
    action: str
    detail: str = ""


class PurchaseJourneyResult(BaseModel):
    findings: list[Finding]
    action_log: list[ActionLogEntry]
    stopped_before_payment: bool = True  # always true - see module docstring


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_price(text: str) -> float | None:
    match = _PRICE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", match.group(0)))
    except ValueError:
        return None


class _JourneyRunner:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.log: list[ActionLogEntry] = []
        self.findings: list[Finding] = []

    def _record(self, action: str, detail: str = "") -> None:
        entry = ActionLogEntry(timestamp=_now(), action=action, detail=detail)
        self.log.append(entry)
        logger.info("[purchase-journey] %s %s", action, f"- {detail}" if detail else "")

    async def run(self, browser: Browser, product_url: str) -> PurchaseJourneyResult:
        try:
            await assert_public_url(product_url)
        except SSRFBlockedError as exc:
            self._record("blocked_ssrf", str(exc))
            self.findings.append(Finding(
                check_id="purchase_journey_blocked_ssrf",
                title="Purchase journey check refused - target resolves to a non-public address",
                severity=Severity.LOW,
                confidence=Confidence.CANNOT_VERIFY,
                evidence=str(exc),
            ))
            return self._finish()

        context = await browser.new_context(
            user_agent=BROWSER_USER_AGENT,
            viewport=STEALTH_VIEWPORT,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        await install_ssrf_guard(context)  # also blocks redirects/subresources to internal addresses
        page = await context.new_page()

        try:
            self._record("navigate_to_product", product_url)
            await page.goto(product_url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            product_price = await self._read_product_price(page)

            clicked = await self._click_add_to_cart(page)
            if not clicked:
                self.findings.append(Finding(
                    check_id="purchase_journey_add_to_cart_failed",
                    title="Could not find an add-to-cart control",
                    severity=Severity.LOW,
                    confidence=Confidence.CANNOT_VERIFY,
                    page_url=product_url,
                    evidence="No recognized add-to-cart button matched on the product page - purchase journey check stopped here.",
                    recommended_fix="Check manually; the theme may use a non-standard add-to-cart control.",
                    location="product page add-to-cart area",
                ))
                return self._finish()

            await self._maybe_wait_idle(page)

            cart_url, cart_text = await self._load_cart(page)
            self._verify_cart(cart_url, cart_text, product_price)

            checkout_url, checkout_text = await self._load_checkout(page)
            if checkout_url:
                self._verify_checkout_totals(checkout_url, checkout_text, product_price)

            # --- HARD STOP ---
            # No further navigation, no clicks, no form submission past this
            # point. See module docstring.
            self._record("stopped_before_payment", "Checkout totals read; no payment-related control was clicked.")
        finally:
            await context.close()

        return self._finish()

    def _finish(self) -> PurchaseJourneyResult:
        return PurchaseJourneyResult(findings=self.findings, action_log=self.log, stopped_before_payment=True)

    async def _read_product_price(self, page) -> float | None:
        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            return None
        price = _extract_price(text)
        self._record("read_product_price", f"{price}" if price is not None else "not found")
        return price

    async def _click_add_to_cart(self, page) -> bool:
        for selector in _ADD_TO_CART_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.click(timeout=5000)
                self._record("click_add_to_cart", f"selector={selector!r}")
                return True
            except Exception as exc:  # noqa: BLE001 - try the next selector
                logger.debug("add-to-cart selector %r failed: %s", selector, exc)
        return False

    async def _maybe_wait_idle(self, page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001 - AJAX add-to-cart may never go fully idle; harmless
            pass

    async def _load_cart(self, page) -> tuple[str | None, str]:
        for path in _CART_PATH_CANDIDATES:
            url = urljoin(self.base_url, path)
            try:
                response = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                if response is not None and response.status < 400:
                    text = await page.inner_text("body")
                    self._record("load_cart_page", url)
                    return url, text
            except Exception as exc:  # noqa: BLE001 - try the next candidate path
                logger.debug("cart path %r failed: %s", url, exc)
        self._record("load_cart_page_failed", "No cart page candidate loaded successfully.")
        self.findings.append(Finding(
            check_id="purchase_journey_cart_unreachable",
            title="Could not load the cart page after adding to cart",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            evidence=f"Tried {_CART_PATH_CANDIDATES} relative to {self.base_url}, none loaded.",
            recommended_fix="Check manually; the store may use a non-standard cart URL.",
            location=None,
        ))
        return None, ""

    def _verify_cart(self, cart_url: str | None, cart_text: str, product_price: float | None) -> None:
        if cart_url is None:
            return
        cart_price = _extract_price(cart_text)
        if product_price is not None and cart_price is not None and abs(product_price - cart_price) > 0.01:
            self.findings.append(Finding(
                check_id="purchase_journey_cart_price_mismatch",
                title="Cart price does not match product page price",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                page_url=cart_url,
                evidence=f"Product page showed {product_price}, cart shows {cart_price}.",
                policy_reference="GMC: Price shown to the customer must be honored through checkout",
                recommended_fix="Investigate pricing rules/coupons/caching causing cart price to differ from the product page.",
                location="cart page line-item price",
            ))
        elif cart_price is None:
            self.findings.append(Finding(
                check_id="purchase_journey_cart_price_not_found",
                title="Could not find a price on the cart page",
                severity=Severity.LOW,
                confidence=Confidence.CANNOT_VERIFY,
                page_url=cart_url,
                evidence="No recognizable price pattern found in the cart page text.",
                recommended_fix="Check manually; the cart may render price via a script this check can't read.",
                location="cart page (no price element found)",
            ))

    async def _load_checkout(self, page) -> tuple[str | None, str]:
        for path in _CHECKOUT_PATH_CANDIDATES:
            url = urljoin(self.base_url, path)
            try:
                response = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                if response is not None and response.status < 400:
                    text = await page.inner_text("body")
                    self._record("load_checkout_page", url)
                    return url, text
            except Exception as exc:  # noqa: BLE001 - try the next candidate path
                logger.debug("checkout path %r failed: %s", url, exc)
        self._record("load_checkout_page_failed", "No checkout page candidate loaded successfully.")
        self.findings.append(Finding(
            check_id="purchase_journey_checkout_unreachable",
            title="Could not load the checkout page",
            severity=Severity.LOW,
            confidence=Confidence.CANNOT_VERIFY,
            evidence=f"Tried {_CHECKOUT_PATH_CANDIDATES} relative to {self.base_url}, none loaded.",
            recommended_fix="Check manually; the store may require login or use a non-standard checkout URL.",
            location=None,
        ))
        return None, ""

    def _verify_checkout_totals(self, checkout_url: str, checkout_text: str, product_price: float | None) -> None:
        lowered = checkout_text.lower()
        has_shipping_line = bool(re.search(r"shipping|delivery", lowered))
        if not has_shipping_line:
            self.findings.append(Finding(
                check_id="purchase_journey_shipping_not_shown",
                title="No shipping charge shown on checkout page",
                severity=Severity.MEDIUM,
                confidence=Confidence.POTENTIAL_RISK,
                page_url=checkout_url,
                evidence="No 'shipping'/'delivery' text found on the checkout page before payment.",
                policy_reference="GMC: Shipping costs must be disclosed before the customer pays",
                recommended_fix="Confirm shipping cost is clearly displayed on checkout for this product/region.",
                location="checkout page (no shipping/delivery line found)",
            ))

        checkout_total = _extract_price(checkout_text)
        if product_price is not None and checkout_total is not None and checkout_total < product_price - 0.01:
            self.findings.append(Finding(
                check_id="purchase_journey_total_lower_than_product_price",
                title="Checkout total is lower than the product page price with no explanation found",
                severity=Severity.MEDIUM,
                confidence=Confidence.POTENTIAL_RISK,
                page_url=checkout_url,
                evidence=f"Product page price {product_price}, lowest price found on checkout page {checkout_total}.",
                recommended_fix="Investigate - may be correct (e.g. a detected discount) or a pricing bug; confirm manually.",
                location="checkout page total/order-summary line",
            ))


async def run_purchase_journey_check(browser: Browser, base_url: str, product_url: str) -> PurchaseJourneyResult:
    runner = _JourneyRunner(base_url)
    return await runner.run(browser, product_url)
