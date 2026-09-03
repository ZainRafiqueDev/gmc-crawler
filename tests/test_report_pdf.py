from pypdf import PdfReader
import io

from PIL import Image

from app.report_pdf import markdown_to_pdf_bytes


def _extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


# --- Annotated screenshots (follow-up round, Part 3) ------------------------

def test_screenshot_line_embeds_a_real_image_when_the_file_exists(tmp_path):
    (tmp_path / "screenshots").mkdir()
    img_path = tmp_path / "screenshots" / "shop-example-llm_prohibited_content-0-20260101-000000.jpg"
    Image.new("RGB", (400, 300), color="red").save(img_path, format="JPEG")

    markdown = "### Some finding\n- **Screenshot:** ![Annotated screenshot for Some finding](screenshots/shop-example-llm_prohibited_content-0-20260101-000000.jpg)\n"
    pdf_bytes = markdown_to_pdf_bytes(markdown, base_dir=tmp_path)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    images = [img for page in reader.pages for img in page.images]

    assert len(images) == 1
    text = _extract_text(pdf_bytes)
    assert "image not available" not in text


def test_screenshot_line_falls_back_to_text_when_file_missing(tmp_path):
    markdown = "- **Screenshot:** ![alt](screenshots/does-not-exist.jpg)\n"
    pdf_bytes = markdown_to_pdf_bytes(markdown, base_dir=tmp_path)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    images = [img for page in reader.pages for img in page.images]

    assert len(images) == 0
    assert "image not available" in _extract_text(pdf_bytes)


def test_converts_headers_and_bullets_to_a_valid_pdf():
    markdown = (
        "# GMC Compliance Audit Report\n\n"
        "## Executive Summary\n\n"
        "- Pages crawled: 5\n"
        "- **Missing required page: Privacy policy**\n"
        "  - Severity: critical | Confidence: confirmed\n"
    )
    pdf_bytes = markdown_to_pdf_bytes(markdown)
    assert pdf_bytes[:5] == b"%PDF-"

    text = _extract_text(pdf_bytes)
    assert "GMC Compliance Audit Report" in text
    assert "Executive Summary" in text
    assert "Pages crawled: 5" in text
    assert "Missing required page: Privacy policy" in text
    assert "Severity: critical | Confidence: confirmed" in text


def test_html_entities_from_sanitize_for_report_are_decoded_not_shown_literally():
    """Same bug class as report_docx.py: sanitize_for_report HTML-escapes
    scraped-content fields for the Markdown/HTML rendering path - a PDF
    isn't HTML, so without unescaping, a literal "&" would show up as the
    literal text "&amp;".
    """
    markdown = (
        "- **Missing terms &amp; conditions**\n"
        "  - Evidence: Price is $5 &amp; up, and &lt;script&gt; shows literally\n"
    )
    pdf_bytes = markdown_to_pdf_bytes(markdown)
    text = _extract_text(pdf_bytes)
    assert "Missing terms & conditions" in text
    assert "Price is $5 & up, and <script> shows literally" in text
    assert "&amp;" not in text
    assert "&lt;" not in text


def test_malicious_script_tag_is_rendered_as_literal_text_not_valid_reportlab_markup():
    """A finding title/evidence lifted from a compromised page could contain
    arbitrary <tags> - these must render as literal visible text, not be
    interpreted as reportlab Paragraph markup (which uses the same tag
    syntax as a small HTML subset) and not raise a parse error either.
    """
    markdown = "- **<script>alert(1)</script> in the title**\n  - Evidence: <b>fake bold</b> attempt\n"
    pdf_bytes = markdown_to_pdf_bytes(markdown)  # must not raise
    text = _extract_text(pdf_bytes)
    assert "<script>alert(1)</script>" in text
    assert "<b>fake bold</b>" in text


def test_bold_leading_span_does_not_bleed_into_rest_of_line():
    markdown = "- **Bold Title** rest of line not bold"
    pdf_bytes = markdown_to_pdf_bytes(markdown)
    text = _extract_text(pdf_bytes)
    assert "Bold Title" in text
    assert "rest of line not bold" in text


def test_empty_lines_are_skipped_and_multiple_pages_supported():
    markdown = "# Title\n\n\nSome text\n" + ("\n- filler line\n" * 5)
    pdf_bytes = markdown_to_pdf_bytes(markdown)
    text = _extract_text(pdf_bytes)
    assert "Title" in text
    assert "Some text" in text


def test_markdown_table_renders_as_a_real_table_not_raw_pipe_text():
    """Regression: app.report's Policy-by-Policy Review matrix is a real
    markdown table, but this exporter used to fall through to plain-
    paragraph rendering for every "|"-prefixed line."""
    markdown = (
        "## Policy-by-Policy Review\n\n"
        "| Policy Area | Status | Findings | Summary |\n"
        "| --- | --- | --- | --- |\n"
        "| Shipping Policy | Pass | 0 | No issues found. |\n"
    )
    pdf_bytes = markdown_to_pdf_bytes(markdown)
    text = _extract_text(pdf_bytes)
    assert "Policy Area" in text
    assert "Shipping Policy" in text
    assert "| --- |" not in text
    assert "| Policy Area |" not in text
