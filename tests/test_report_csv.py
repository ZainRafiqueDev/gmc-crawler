"""Unit tests for the full-detail CSV export (report-bloat follow-up round,
Part 3) - the escape hatch that must contain every raw finding instance the
check pipeline produced, unaggregated, confirming aggregation never drops
data, only re-presents it.
"""
from __future__ import annotations

import csv
import io

from app.models import Confidence, Finding, Severity
from app.report_csv import findings_to_csv_bytes


def _finding(i: int) -> Finding:
    return Finding(
        check_id="external_domain_link", title="External-domain link found", severity=Severity.LOW,
        confidence=Confidence.CONFIRMED, page_url=f"https://x.example/p{i}",
        evidence=f"Link to https://social.example found on https://x.example/p{i}",
        location='a[href="https://social.example"]',
    )


def _rows(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_one_row_per_raw_finding_instance_no_aggregation():
    findings = [_finding(i) for i in range(50)]
    rows = _rows(findings_to_csv_bytes(findings))
    assert len(rows) == 50


def test_row_contains_the_real_page_url_and_evidence():
    rows = _rows(findings_to_csv_bytes([_finding(0)]))
    assert rows[0]["page_url"] == "https://x.example/p0"
    assert "social.example" in rows[0]["evidence"]
    assert rows[0]["check_id"] == "external_domain_link"
    assert rows[0]["severity"] == "low"
    assert rows[0]["confidence"] == "confirmed"


def test_empty_findings_list_produces_header_only():
    rows = _rows(findings_to_csv_bytes([]))
    assert rows == []


def test_none_fields_render_as_empty_string_not_the_word_none():
    f = Finding(check_id="x", title="t", severity=Severity.LOW, confidence=Confidence.CONFIRMED, evidence="e")
    rows = _rows(findings_to_csv_bytes([f]))
    assert rows[0]["page_url"] == ""
    assert rows[0]["location"] == ""
    assert rows[0]["screenshot_path"] == ""
