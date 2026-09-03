"""Structural checks on <form> elements found during the crawl - contact
forms and newsletter/signup forms specifically. Deliberately NEVER submits
a real form: submitting a real contact form on a live store generates an
actual email/ticket to the merchant, the same category of real-world side
effect the Phase E purchase-journey checks are careful to avoid. Every check
here is read-only: parse the already-fetched HTML, and at most a GET against
the form's declared action URL to confirm it isn't 404/5xx - never a POST,
never real field data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.fetch import FAILURE_CATEGORY_RECOMMENDATIONS, FAILURE_CATEGORY_SHORT_LABELS, classify_httpx_exception
from app.models import Confidence, CrawledPage, Finding, PageType, Severity, SiteMap
from app.security.ssrf_guard import DNSResolutionError, SSRFBlockedError, safe_async_client

_FORM_FETCH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_NAME_FIELD_RE = re.compile(r"name|full[-_ ]?name|your[-_ ]?name|first[-_ ]?name", re.IGNORECASE)
_EMAIL_FIELD_RE = re.compile(r"e-?mail", re.IGNORECASE)
_MESSAGE_FIELD_RE = re.compile(r"message|comment|enquiry|inquiry|details", re.IGNORECASE)
_NEWSLETTER_HINT_RE = re.compile(r"newsletter|subscribe|signup|sign-up|sign_up", re.IGNORECASE)
_CONTACT_HINT_RE = re.compile(r"contact|enquiry|inquiry", re.IGNORECASE)

# Field types that never represent user-entered lead-gen data - a form made
# up only of these (e.g. a quantity selector, a hidden CSRF token, a plain
# search box) isn't a contact/signup form and should be silently skipped
# rather than flagged as a "broken contact form".
_NON_LEADGEN_INPUT_TYPES = {"hidden", "submit", "button", "search", "checkbox", "radio", "file"}


@dataclass
class _Field:
    kind: str  # input type, or "textarea"
    hint: str  # name/id/placeholder/label text - used to guess the field's purpose
    element: Tag


def _field_hint(el: Tag) -> str:
    """Everything about an input/textarea that might reveal its purpose -
    name, id, placeholder, and any associated <label> text."""
    parts = [el.get("name", ""), el.get("id", ""), el.get("placeholder", "")]
    label = el.find_parent("label")
    if label:
        parts.append(label.get_text(" ", strip=True))
    return " ".join(p for p in parts if p)


def _extract_fields(form: Tag) -> list[_Field]:
    fields: list[_Field] = []
    for el in form.find_all(["input", "textarea"]):
        input_type = (el.get("type") or "text").lower() if el.name == "input" else "textarea"
        if input_type in _NON_LEADGEN_INPUT_TYPES:
            continue
        fields.append(_Field(kind=input_type, hint=_field_hint(el), element=el))
    return fields


def _is_email_field(f: _Field) -> bool:
    return f.kind == "email" or bool(_EMAIL_FIELD_RE.search(f.hint))


def _is_name_field(f: _Field) -> bool:
    return f.kind == "text" and bool(_NAME_FIELD_RE.search(f.hint)) and not _EMAIL_FIELD_RE.search(f.hint)


def _is_message_field(f: _Field) -> bool:
    return f.kind == "textarea" or bool(_MESSAGE_FIELD_RE.search(f.hint))


_ACCOUNT_FORM_HINT_RE = re.compile(r"login|register|log-in|sign-in|signin", re.IGNORECASE)

# WordPress core's native product-review/comment form (rendered on every
# WooCommerce product page with reviews enabled - not a per-theme
# customization, so this hits any WooCommerce store, not just one). Its
# "Name"/"Email" fields (id="author"/id="email") look exactly like a
# 2-3 field contact form (has_email + has_message via the "Your review"
# textarea) to the generic contact-form heuristic below - and its "Name"
# field's own name/id attribute is literally "author", not "name", so
# _is_name_field never matches it either (WP core wraps each field in a
# <p> with a *sibling* <label for="author">Name</label>, not a label that
# wraps the input as a parent - _field_hint only walks up to a parent
# label, so that "Name" text is never picked up). The combination produced
# two false suspension-risk findings per product page on a real live
# store (vellano.site): "missing a name field" (never true - the field
# exists, this check just can't recognize it) and "submission endpoint
# returns an error" (wp-comments-post.php legitimately 403s a plain GET -
# it's POST-only by WordPress core design, not a broken endpoint).
# Excluded the same way the login/register form already is: a reliable
# id/class/action signature, checked before any classification logic runs.
_REVIEW_FORM_HINT_RE = re.compile(r"commentform|comment-form|wp-comments-post\.php", re.IGNORECASE)


def _classify_form(page: CrawledPage, form: Tag, fields: list[_Field]) -> str | None:
    """'contact', 'newsletter', or None (not a lead-gen form worth checking -
    e.g. search, login/register/account, a product review/comment form, a
    product quantity/add-to-cart form). A login form's "username or email"
    label can otherwise look exactly like a 2-field newsletter signup (an
    "email"-ish field plus one more) - verified live against a real
    WooCommerce login form - so a password field or a login/register
    class/id is a hard exclusion, checked before anything else. A native
    WordPress review/comment form is excluded the same way - see
    _REVIEW_FORM_HINT_RE's comment for the live false-positive this fixes.
    """
    if any(f.kind == "password" for f in fields):
        return None
    # form.get("class") returns every individual class token as a list
    # (BeautifulSoup splits the class attribute) - joining all of them, not
    # just the first, matters live: WooCommerce's register form is
    # class="woocommerce-form woocommerce-form-register register", where
    # the identifying "register" token isn't first.
    css_class = " ".join(form.get("class") or [])
    submit_text = " ".join(
        el.get("value", "") if el.name == "input" else el.get_text(" ", strip=True)
        for el in form.find_all(["input", "button"])
        if (el.get("type") or "").lower() == "submit" or el.name == "button"
    )
    form_hint = " ".join([form.get("id", ""), css_class, form.get("action", ""), submit_text])
    if _ACCOUNT_FORM_HINT_RE.search(form_hint):
        return None
    if _REVIEW_FORM_HINT_RE.search(form_hint):
        return None

    has_email = any(_is_email_field(f) for f in fields)
    has_message = any(_is_message_field(f) for f in fields)

    # An *explicit* newsletter signal (id/class/action/submit-button-text
    # says "newsletter"/"subscribe"/"signup") is checked first and
    # independent of page_type: a sitewide footer newsletter widget renders
    # on every page including /contact-us and /about-us, so "this page is
    # CONTACT_ABOUT" is not reliable evidence the form on it is actually a
    # contact form - verified live against a real store's footer newsletter
    # form ("SUBSCRIBE" button, single your-email field) that page_type
    # alone would have misclassified as a contact form missing name/message.
    if _NEWSLETTER_HINT_RE.search(form_hint) and has_email:
        return "newsletter"

    if page.page_type == PageType.CONTACT_ABOUT and has_email:
        return "contact"
    if _CONTACT_HINT_RE.search(form_hint) and has_email:
        return "contact"
    if has_email and has_message and len(fields) >= 2:
        return "contact"
    # Generic newsletter fallback, checked last: only reached once nothing
    # above suggested contact intent (no contact page, no contact hint, no
    # message field) - e.g. a bare header/footer email-only signup with no
    # explicit "newsletter" wording anywhere.
    if has_email and len(fields) <= 2 and not has_message:
        return "newsletter"
    return None


def _check_contact_form_fields(page: CrawledPage, fields: list[_Field]) -> list[Finding]:
    has_name = any(_is_name_field(f) for f in fields)
    has_email = any(_is_email_field(f) for f in fields)
    has_message = any(_is_message_field(f) for f in fields)

    missing = [label for label, present in [("name", has_name), ("email", has_email), ("message", has_message)] if not present]
    if not missing:
        return []
    return [Finding(
        check_id="contact_form_missing_field",
        title=f"Contact form is missing a {'/'.join(missing)} field",
        severity=Severity.MEDIUM,
        confidence=Confidence.CONFIRMED,
        page_url=page.url,
        evidence=f"Contact form on {page.url} has no recognizable {'/'.join(missing)} field.",
        policy_reference="GMC: Business must be reachable through the contact information provided",
        recommended_fix=f"Add a {'/'.join(missing)} field so customers can be reliably identified and followed up with.",
        location="form (contact)",
    )]


def _check_email_field_validation(page: CrawledPage, fields: list[_Field], form_label: str) -> list[Finding]:
    """Soft heuristic, not a certainty (server-side validation, if any,
    isn't visible to us) - flagged as potential_risk: an email field that
    is neither type="email" nor has a "required"/"pattern" attribute may
    silently accept obviously-invalid input (blank, or not email-shaped)
    client-side.
    """
    for f in fields:
        if not _is_email_field(f):
            continue
        el = f.element
        has_email_type = f.kind == "email"
        has_constraint = el.has_attr("required") or el.has_attr("pattern")
        if has_email_type or has_constraint:
            continue
        return [Finding(
            check_id="form_email_field_weak_validation",
            title=f"{form_label.capitalize()} form's email field has no client-side validation",
            severity=Severity.LOW,
            confidence=Confidence.POTENTIAL_RISK,
            page_url=page.url,
            evidence=(
                f"Email-labeled field on {page.url} is type={f.kind!r} with no required/pattern "
                f"attribute - it may accept blank or obviously-invalid input."
            ),
            recommended_fix='Mark the email field type="email" and/or add a required attribute.',
            location=f"form ({form_label}) email field",
        )]
    return []


def _normalize_for_comparison(url: str) -> str:
    """Strips the fragment and a trailing slash, so a same-page action like
    "/about-us/#wpcf7-f210-o1" compares equal to the crawler's own
    already-normalized page.url ("https://site.example/about-us", no
    trailing slash - see app/site_mapper.py's _normalize) - verified live:
    without the trailing-slash strip, this comparison silently failed to
    recognize the two as the same page and still made the false-positive
    network call this function exists to avoid.
    """
    parsed = urlparse(url)._replace(fragment="")
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        parsed = parsed._replace(path=path.rstrip("/"))
    return urlunparse(parsed)


async def _check_action_reachable(page: CrawledPage, form: Tag, form_label: str) -> list[Finding]:
    action = (form.get("action") or "").strip()
    if not action or action.startswith(("mailto:", "javascript:", "#", "tel:")):
        return []  # self-submitting or non-HTTP action - nothing to probe

    target = urljoin(page.url, action)
    if not target.startswith(("http://", "https://")):
        return []

    # A same-page anchor (e.g. Contact Form 7's "/current-page/#wpcf7-fN-oM" -
    # a client-side scroll target its own JS uses after an AJAX submit, not a
    # real distinct endpoint) tells us nothing we don't already know: we just
    # crawled this exact page successfully. Probing it anyway produced a live
    # false positive - a WAF/bot-protection 403 on a plain GET to a page a
    # real browser (Playwright, used for the actual crawl) loaded fine -
    # verified against a real store's Contact Form 7 form.
    if _normalize_for_comparison(target) == _normalize_for_comparison(page.url):
        return []

    try:
        async with safe_async_client(timeout=_FORM_FETCH_TIMEOUT) as client:
            resp = await client.get(target, follow_redirects=True)
    except DNSResolutionError as exc:
        # Caught ahead of SSRFBlockedError deliberately (DNSResolutionError
        # subclasses it): a resolver hiccup is a reliability failure, not a
        # confirmed non-public-address block - conflating the two here would
        # reproduce, on a form's action URL, the exact false-positive class
        # already found and fixed for page fetches in app/fetch.py (a
        # transient DNS blip on britanniagifts.us was reported as a
        # confirmed SSRF block instead of "could not verify").
        return [Finding(
            check_id="form_action_unreachable",
            title=f"{form_label.capitalize()} form submission endpoint could not be verified (network error)",
            severity=Severity.MEDIUM,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence=f"GET {target} - {FAILURE_CATEGORY_SHORT_LABELS['network_error']}: {exc}",
            recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS["network_error"],
            location=f"form ({form_label})",
        )]
    except SSRFBlockedError as exc:
        return [Finding(
            check_id="form_action_unreachable",
            title=f"{form_label.capitalize()} form submits to a blocked/non-public address",
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            page_url=page.url,
            evidence=f"Form action {action!r} resolves to a non-public address: {exc}",
            recommended_fix="Point the form's action at a real, public submission endpoint.",
            location=f"form ({form_label})",
        )]
    except httpx.HTTPError as exc:
        category = classify_httpx_exception(exc)
        reason = FAILURE_CATEGORY_SHORT_LABELS[category]
        return [Finding(
            check_id="form_action_unreachable",
            title=f"{form_label.capitalize()} form submission endpoint could not be reached ({reason})",
            severity=Severity.MEDIUM,
            confidence=Confidence.CANNOT_VERIFY,
            page_url=page.url,
            evidence=f"GET {target} failed - {reason}: {exc}",
            recommended_fix=FAILURE_CATEGORY_RECOMMENDATIONS[category],
            location=f"form ({form_label})",
        )]

    if resp.status_code >= 400:
        return [Finding(
            check_id="form_action_unreachable",
            title=f"{form_label.capitalize()} form submission endpoint returns an error",
            severity=Severity.MEDIUM,
            confidence=Confidence.CONFIRMED,
            page_url=page.url,
            evidence=f"GET {target} returned HTTP {resp.status_code}.",
            recommended_fix="Fix the form's action URL - customers submitting it will likely see the same error.",
            location=f"form ({form_label})",
        )]
    return []


async def check_forms(site_map: SiteMap) -> list[Finding]:
    """Structural, read-only checks on every contact/newsletter form found
    during the crawl. No form is ever submitted - see module docstring.
    """
    findings: list[Finding] = []
    for page in site_map.pages:
        if not page.reachable or not page.html:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        for form in soup.find_all("form"):
            fields = _extract_fields(form)
            if not fields:
                continue
            form_label = _classify_form(page, form, fields)
            if form_label is None:
                continue

            if form_label == "contact":
                findings.extend(_check_contact_form_fields(page, fields))
            findings.extend(_check_email_field_validation(page, fields, form_label))
            findings.extend(await _check_action_reachable(page, form, form_label))

    return findings
