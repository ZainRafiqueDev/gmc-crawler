import io

import httpx
import pytest
import respx
from PIL import Image

from app.checks.product_images import (
    MIN_DIMENSION_PX,
    ProductImage,
    _probe_image,
    images_from_page_html,
    run_deterministic_image_checks,
)
from app.models import Confidence, CrawledPage, PageType, Severity, VerificationMethod


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="red").save(buf, format="PNG")
    return buf.getvalue()


def test_images_from_page_html_extracts_url_and_alt():
    html = '<html><body><img src="/img/widget.jpg" alt="Red Widget"></body></html>'
    page = CrawledPage(url="https://shop.example/products/widget", page_type=PageType.PRODUCT, depth=1, reachable=True, html=html)
    images = images_from_page_html(page)
    assert len(images) == 1
    assert images[0].url == "https://shop.example/img/widget.jpg"
    assert images[0].alt_text == "Red Widget"
    assert images[0].source == VerificationMethod.PAGE_ONLY


def test_images_from_page_html_missing_alt():
    html = '<html><body><img src="/img/widget.jpg"></body></html>'
    page = CrawledPage(url="https://shop.example/products/widget", page_type=PageType.PRODUCT, depth=1, reachable=True, html=html)
    images = images_from_page_html(page)
    assert images[0].alt_text is None


@pytest.mark.asyncio
@respx.mock
async def test_flags_placeholder_filename():
    img = ProductImage(url="https://shop.example/img/placeholder.jpg", alt_text="ok", source=VerificationMethod.PAGE_ONLY)
    respx.get(img.url).mock(return_value=httpx.Response(200, content=_png_bytes(500, 500), headers={"content-type": "image/png"}))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    assert any(f.check_id == "product_image_placeholder_filename" for f in findings)


@pytest.mark.asyncio
@respx.mock
async def test_flags_missing_alt_text():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text=None, source=VerificationMethod.API_VERIFIED)
    respx.get(img.url).mock(return_value=httpx.Response(200, content=_png_bytes(500, 500), headers={"content-type": "image/png"}))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    alt_findings = [f for f in findings if f.check_id == "product_image_missing_alt_text"]
    assert len(alt_findings) == 1
    assert alt_findings[0].verification_method == VerificationMethod.API_VERIFIED


@pytest.mark.asyncio
@respx.mock
async def test_flags_broken_image():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text="ok", source=VerificationMethod.PAGE_ONLY)
    respx.get(img.url).mock(return_value=httpx.Response(404))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    broken = [f for f in findings if f.check_id == "product_image_broken"]
    assert len(broken) == 1
    assert broken[0].confidence == Confidence.CONFIRMED
    assert "404" in broken[0].evidence


@pytest.mark.asyncio
@respx.mock
async def test_flags_low_resolution():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text="ok", source=VerificationMethod.PAGE_ONLY)
    respx.get(img.url).mock(return_value=httpx.Response(200, content=_png_bytes(80, 80), headers={"content-type": "image/png"}))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    low_res = [f for f in findings if f.check_id == "product_image_low_resolution"]
    assert len(low_res) == 1
    assert "80x80" in low_res[0].evidence
    assert str(MIN_DIMENSION_PX) in low_res[0].evidence


@pytest.mark.asyncio
@respx.mock
async def test_no_findings_for_good_image():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text="Widget photo", source=VerificationMethod.API_VERIFIED)
    respx.get(img.url).mock(return_value=httpx.Response(200, content=_png_bytes(600, 600), headers={"content-type": "image/png"}))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    assert findings == []


@pytest.mark.asyncio
async def test_empty_page_images_returns_empty():
    findings = await run_deterministic_image_checks({})
    assert findings == []


# --- Failure-reporting specificity (follow-up round, Part 1.3) -------------

@pytest.mark.asyncio
@respx.mock
async def test_network_failure_names_a_specific_category_not_a_blocking_guess():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text="ok", source=VerificationMethod.PAGE_ONLY)
    respx.get(img.url).mock(side_effect=httpx.ConnectTimeout("connect timed out"))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    broken = [f for f in findings if f.check_id == "product_image_broken"]
    assert len(broken) == 1
    assert broken[0].confidence == Confidence.CANNOT_VERIFY
    # Not the old blanket "may be blocking automated requests" guess - a
    # real network_error category with its own accurate recommendation.
    assert "network error" in broken[0].title.lower()
    assert "blocking automated requests" not in broken[0].recommended_fix
    assert "DNS/connectivity" in broken[0].recommended_fix


@pytest.mark.asyncio
@respx.mock
async def test_undecodable_image_gets_a_data_quality_reason_not_a_blocking_guess():
    img = ProductImage(url="https://shop.example/img/widget.jpg", alt_text="ok", source=VerificationMethod.PAGE_ONLY)
    respx.get(img.url).mock(return_value=httpx.Response(200, content=b"not actually an image"))
    findings = await run_deterministic_image_checks({"https://shop.example/products/widget": [img]})
    broken = [f for f in findings if f.check_id == "product_image_broken"]
    assert len(broken) == 1
    assert broken[0].confidence == Confidence.CANNOT_VERIFY
    assert "invalid or oversized image data" in broken[0].title
    assert "blocking automated requests" not in broken[0].recommended_fix
    assert "could not decode" in broken[0].evidence


@pytest.mark.asyncio
@respx.mock
async def test_probe_image_aborts_when_content_length_exceeds_max():
    url = "https://shop.example/img/huge.jpg"
    respx.get(url).mock(return_value=httpx.Response(200, content=_png_bytes(10, 10), headers={"content-length": "999999999"}))
    async with httpx.AsyncClient() as client:
        status, size, error, _category = await _probe_image(client, url, max_bytes=1024)
    assert size is None
    assert "exceeds max size" in error


@pytest.mark.asyncio
@respx.mock
async def test_probe_image_aborts_mid_stream_when_body_exceeds_max_despite_no_content_length_header():
    url = "https://shop.example/img/huge2.jpg"
    big_body = _png_bytes(2000, 2000)  # a real, valid, larger-than-cap PNG
    respx.get(url).mock(return_value=httpx.Response(200, content=big_body))  # no content-length header set by respx here
    async with httpx.AsyncClient() as client:
        status, size, error, _category = await _probe_image(client, url, max_bytes=100)
    assert size is None
    assert "exceeds max size" in error


@pytest.mark.asyncio
@respx.mock
async def test_probe_image_succeeds_within_size_cap():
    url = "https://shop.example/img/ok.jpg"
    respx.get(url).mock(return_value=httpx.Response(200, content=_png_bytes(50, 50)))
    async with httpx.AsyncClient() as client:
        status, size, error, _category = await _probe_image(client, url, max_bytes=1_000_000)
    assert size == (50, 50)
    assert error is None
