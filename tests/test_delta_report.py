from app.models import Confidence, Finding, Platform, PlatformDetectionResult, Severity, SiteMap
from app.report import generate_delta_report


def _finding(check_id, page_url, evidence, severity=Severity.MEDIUM):
    return Finding(
        check_id=check_id, title=check_id, severity=severity, confidence=Confidence.CONFIRMED,
        page_url=page_url, evidence=evidence,
    )


def _platform():
    return PlatformDetectionResult(platform=Platform.WOOCOMMERCE, base_url="https://x.example/", evidence=[])


def _site_map():
    return SiteMap(base_url="https://x.example/", pages=[])


def test_new_issue_detected():
    previous = []
    current = [_finding("https_enforced", "https://x.example/", "not https")]
    report = generate_delta_report(_platform(), _site_map(), previous, current)
    assert "New issues: 1" in report
    assert "Resolved issues: 0" in report
    assert "https_enforced" in report or "not https" in report


def test_resolved_issue_detected():
    previous = [_finding("broken_internal_link", "https://x.example/gone", "404")]
    current = []
    report = generate_delta_report(_platform(), _site_map(), previous, current)
    assert "Resolved issues: 1" in report
    assert "no longer detected" in report


def test_changed_issue_detected_when_evidence_differs():
    previous = [_finding("woocommerce_price_mismatch", "https://x.example/product/widget", "API=19.99, page=24.99")]
    current = [_finding("woocommerce_price_mismatch", "https://x.example/product/widget", "API=19.99, page=29.99")]
    report = generate_delta_report(_platform(), _site_map(), previous, current)
    assert "Changed issues: 1" in report
    assert "24.99" in report and "29.99" in report


def test_unchanged_issue_not_listed_as_new_or_resolved():
    f = _finding("required_page_present", None, "missing privacy policy")
    report = generate_delta_report(_platform(), _site_map(), [f], [f])
    assert "New issues: 0" in report
    assert "Resolved issues: 0" in report
    assert "Changed issues: 0" in report
    assert "Unchanged issues: 1" in report


def test_severity_change_counts_as_changed():
    previous = [_finding("external_domain_link", "https://x.example/", "link found", severity=Severity.LOW)]
    current = [_finding("external_domain_link", "https://x.example/", "link found", severity=Severity.HIGH)]
    report = generate_delta_report(_platform(), _site_map(), previous, current)
    assert "Changed issues: 1" in report
