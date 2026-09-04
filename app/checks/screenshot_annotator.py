"""Annotated screenshots for Suspension Risk Findings (follow-up round,
Part 3). Scope already confirmed, not new this round: only Suspension Risk
Findings get a screenshot attempt - a site-wide aggregate finding (e.g. a
business-identity inconsistency spanning several pages) has no single
"the" element to highlight and is skipped entirely, not given a partial/
misleading exhibit. In practice this means only LLM-graded findings
(check_id starting "llm_", vision checks excluded - see
_is_screenshot_eligible) are attempted: deterministic findings already carry
a real CSS selector in Finding.location, so they never needed this at all,
and every LLM-graded finding here already carries its own model-produced,
schema-required verbatim evidence_quote (never invented for this feature -
the model is never asked for a selector, only ever for the quote it already
has to produce anyway).

Capture happens via a lightweight SECOND visit per distinct page_url, not
inline during the original crawl pass: app.fetch.PageFetcher closes its
browser context after every single fetch (no live Page object survives the
crawl to screenshot later), and only a small fraction of pages ever produce
a suspension-risk LLM finding in practice - revisiting just those specific
URLs is far cheaper than keeping every page's context open through the
whole pipeline just in case.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import Browser, TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.fetch import BROWSER_USER_AGENT, STEALTH_INIT_SCRIPT, STEALTH_VIEWPORT
from app.models import Finding
from app.report import is_suspension_risk_finding
from app.security.ssrf_guard import install_ssrf_guard

logger = logging.getLogger("gmc_audit.checks.screenshot_annotator")

_NAV_TIMEOUT_MS = 20_000
_SETTLE_TIMEOUT_MS = 5_000
_CROP_MARGIN_PX = 40
_MAX_SCREENSHOT_WIDTH_PX = 1200
_JPEG_QUALITY = 82

# LLM checks that grade an image (not page text) have nothing for a
# text-quote DOM search to find - excluded, not just naturally non-matching.
_TEXT_QUOTE_CHECK_PREFIX = "llm_"
_EXCLUDED_LLM_CHECKS = {"llm_image_vision_check"}

_QUOTE_RE = re.compile(r'"([^"]{8,})"')

# Finds the smallest element whose normalized text contains the target quote
# (case/whitespace-insensitive - a model-produced quote may differ from the
# live DOM in whitespace collapsing) and marks it, returning its bounding
# box in page coordinates. Prefers a leaf-ish element (no children) first so
# the highlight/crop is as tight as possible; falls back to the smallest
# matching element of any kind if no leaf matches.
#
# scrollIntoView() only *requests* a scroll; plain (no-`behavior`) calls
# inherit the page's CSS `scroll-behavior`, and a `scroll-behavior: smooth`
# page (found live - a common modern WordPress/Elementor theme default)
# animates the scroll over ~800ms instead of applying it immediately, so
# reading getBoundingClientRect() right after - even after a couple of
# animation frames - still returns the element's pre-scroll position (found
# live: a box hundreds of pixels below the viewport for an element
# scrollIntoView({block:"center"}) should have centered, on a page confirmed
# to contain the quote verbatim - the search itself worked, but the returned
# coordinates were mid-animation/stale). Explicitly passing
# behavior:"instant" overrides the page's CSS default for this one call and
# applies synchronously, confirmed live against the exact page/quote that
# exposed this - no arbitrary sleep needed.
_FIND_QUOTE_JS = """
(quote) => {
    const norm = s => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
    const target = norm(quote);
    if (!target) return null;
    const all = Array.from(document.body.querySelectorAll("*"));
    let leafMatch = null;
    let smallestAny = null;
    for (const el of all) {
        const text = norm(el.textContent);
        if (!text || !text.includes(target)) continue;
        if (el.children.length === 0 && !leafMatch) {
            leafMatch = el;
        }
        if (!smallestAny || el.textContent.length < smallestAny.textContent.length) {
            smallestAny = el;
        }
    }
    const best = leafMatch || smallestAny;
    if (!best) return null;
    best.style.outline = "4px solid #dc2626";
    best.style.outlineOffset = "2px";
    best.style.boxShadow = "0 0 0 4px rgba(220, 38, 38, 0.25)";
    best.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    const rect = best.getBoundingClientRect();
    return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
}
"""


def _is_screenshot_eligible(f: Finding) -> bool:
    return (
        is_suspension_risk_finding(f)
        and f.check_id.startswith(_TEXT_QUOTE_CHECK_PREFIX)
        and f.check_id not in _EXCLUDED_LLM_CHECKS
        and f.page_url is not None
        and bool(f.evidence and f.evidence.strip())
    )


def _candidate_quotes(evidence: str) -> list[str]:
    """Most findings' evidence IS the verbatim quote directly - tried last,
    since a composite finding's evidence (e.g. claim-vs-policy contradiction,
    which embeds two quotes in one templated sentence) would otherwise match
    the whole templated string, not a real DOM location. Quoted substrings
    extracted first are tried in order for exactly that case.
    """
    quoted = _QUOTE_RE.findall(evidence)
    candidates = [*quoted, evidence]
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _safe_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len] or "finding"


async def _capture_one(page, quotes: list[str], out_path: Path) -> bool:
    for quote in quotes:
        try:
            box = await page.evaluate(_FIND_QUOTE_JS, quote)
        except Exception as exc:  # noqa: BLE001 - a DOM-search failure on one candidate just means "try the next"
            logger.debug("Quote-location JS failed for one candidate: %s", exc)
            continue
        if not box:
            continue

        viewport = page.viewport_size or STEALTH_VIEWPORT
        clip = {
            "x": max(0, box["x"] - _CROP_MARGIN_PX),
            "y": max(0, box["y"] - _CROP_MARGIN_PX),
        }
        clip["width"] = min(box["width"] + 2 * _CROP_MARGIN_PX, viewport["width"] - clip["x"])
        clip["height"] = min(box["height"] + 2 * _CROP_MARGIN_PX, viewport["height"] - clip["y"])
        if clip["width"] <= 0 or clip["height"] <= 0:
            continue

        try:
            png_bytes = await page.screenshot(clip=clip)
        except Exception as exc:  # noqa: BLE001 - a clip outside the actual rendered surface degrades to "skip", not a crash
            logger.warning("Screenshot capture failed after locating quote: %s", exc)
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(png_bytes)) as im:
            im = im.convert("RGB")
            if im.width > _MAX_SCREENSHOT_WIDTH_PX:
                ratio = _MAX_SCREENSHOT_WIDTH_PX / im.width
                im = im.resize((_MAX_SCREENSHOT_WIDTH_PX, max(1, int(im.height * ratio))))
            im.save(out_path, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return True
    return False


async def capture_annotated_screenshots(
    browser: Browser, findings: list[Finding], settings: Settings, report_output_dir: Path, filename_prefix: str = "site",
) -> list[Finding]:
    """Non-mutating - same pattern as apply_impact_tiers/apply_ads_eligibility_impact:
    returns a new list, every finding present, only screenshot-eligible ones
    that were actually successfully located get screenshot_path set.
    Finding.screenshot_path is stored relative to report_output_dir (not an
    absolute filesystem path) so the report itself, and its docx/PDF
    exports, can resolve it regardless of where report_output_dir happens
    to be mounted on a given deployment. filename_prefix (the store's own
    safe-for-filenames host) keeps filenames from colliding across stores
    audited into the same report_output_dir.
    """
    eligible = [f for f in findings if _is_screenshot_eligible(f)]
    if not eligible:
        return findings

    by_page: dict[str, list[Finding]] = {}
    for f in eligible:
        by_page.setdefault(f.page_url, []).append(f)

    updates: dict[int, str] = {}  # id(finding) -> relative screenshot path
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    for page_url, page_findings in by_page.items():
        context = await browser.new_context(
            user_agent=BROWSER_USER_AGENT, viewport=STEALTH_VIEWPORT,
            locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        await install_ssrf_guard(context)
        page = await context.new_page()
        try:
            try:
                await page.goto(page_url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 - a failed second visit just means no screenshots for this page's findings, not an audit failure
                logger.warning("Screenshot second-visit failed for %s: %s", page_url, exc)
                continue

            # Found live: this quote-location step (getBoundingClientRect
            # right after domcontentloaded) intermittently missed a quote
            # later confirmed to be verbatim on the page - late webfont/
            # image-driven reflow can still shift element positions (and even
            # what a "smallest matching element" resolves to) for a moment
            # after domcontentloaded fires. PageFetcher already waits for
            # networkidle-with-a-bounded-fallback for exactly this reason
            # (see PageFetcher._map... nav flow in app/fetch.py); this second,
            # lightweight visit had none of that. Bounded and falls back to
            # the domcontentloaded snapshot on timeout, same as PageFetcher.
            try:
                await page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.debug("networkidle timed out for screenshot second-visit to %s - using domcontentloaded snapshot", page_url)

            for i, f in enumerate(page_findings):
                quotes = _candidate_quotes(f.evidence)
                filename = f"{filename_prefix}-{_safe_slug(urlparse(page_url).path)}-{f.check_id}-{i}-{timestamp}.jpg"
                relative_path = f"screenshots/{filename}"
                out_path = report_output_dir / relative_path
                found = await _capture_one(page, quotes, out_path)
                if found:
                    updates[id(f)] = relative_path
                else:
                    logger.info("Could not locate evidence quote in the rendered page for %s on %s - skipping its screenshot", f.check_id, page_url)
        finally:
            await context.close()

    if not updates:
        return findings

    return [f.model_copy(update={"screenshot_path": updates[id(f)]}) if id(f) in updates else f for f in findings]
