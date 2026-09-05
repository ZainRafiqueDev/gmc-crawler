"""Unit tests for the report-bloat follow-up round's aggregation pass
(app/finding_aggregation.py). Generalizes app.checks.business_identity.
check_business_identity_consistency's existing pattern (one Finding per
distinct value, with a page list in its evidence) to the high-volume checks
found live to actually drive report bloat on a real store
(britanniagifts.us: 296 "internal http:// link" findings collapsed to
exactly 2 distinct links; 81 "broken image" findings were 81 almost
entirely distinct image URLs, not one repeated - see this module's own
docstring for why that shape difference matters for the strategy chosen
per check_id).
"""
from __future__ import annotations

from app.finding_aggregation import aggregate_repetitive_findings, is_aggregatable_check_id
from app.models import Confidence, Finding, Severity


def _link_finding(page_url: str, link: str, **overrides) -> Finding:
    """https_mixed_content_link: found live to be a genuinely identical
    hardcoded URL repeated verbatim (no per-page variation) - aggregated by
    exact href."""
    defaults = dict(
        check_id="https_mixed_content_link", title="Internal link points to http:// instead of https://",
        severity=Severity.LOW, confidence=Confidence.CONFIRMED, page_url=page_url,
        evidence=f"Link to {link} found on {page_url}", location=f'a[href="{link}"]',
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _social_link_finding(page_url: str, link: str, **overrides) -> Finding:
    """external_domain_link: found live to usually be a social-share button
    whose href embeds the current page's own URL/image/description as query
    params - a different literal string on every page even though it's the
    same share button - aggregated by domain instead of exact href."""
    defaults = dict(
        check_id="external_domain_link", title="External-domain link found", severity=Severity.LOW,
        confidence=Confidence.CONFIRMED, page_url=page_url, evidence=f"Link to {link} found on {page_url}",
        location=f'a[href="{link}"]',
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _image_finding(page_url: str, img: str, check_id: str = "product_image_missing_alt_text", **overrides) -> Finding:
    defaults = dict(
        check_id=check_id, title="Product image missing alt text", severity=Severity.LOW,
        confidence=Confidence.CONFIRMED, page_url=page_url, evidence=f"Image {img} has no alt text.",
        location=f'img[src="{img}"]',
    )
    defaults.update(overrides)
    return Finding(**defaults)


# --- Link checks: aggregate by the specific distinct value ------------------

def test_same_link_across_many_pages_collapses_to_one_finding():
    findings = [_link_finding(f"https://x.example/page{i}", "http://x.example/shop") for i in range(5)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 1
    agg = result[0]
    assert agg.check_id == "https_mixed_content_link"
    assert agg.page_url is None
    assert "http://x.example/shop" in agg.evidence
    for i in range(5):
        assert f"https://x.example/page{i}" in agg.evidence


def test_two_distinct_links_each_repeated_produce_two_separate_aggregates():
    findings = (
        [_link_finding(f"https://x.example/p{i}", "http://x.example/shop") for i in range(4)]
        + [_link_finding(f"https://x.example/p{i}", "http://x.example/") for i in range(4)]
    )
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 2
    values = {f.location for f in result}
    assert values == {'a[href="http://x.example/shop"]', 'a[href="http://x.example/"]'}


def test_below_threshold_group_is_left_unaggregated():
    """Only 2 occurrences - aggregating wouldn't help; stays as 2 individual findings."""
    findings = [_link_finding(f"https://x.example/p{i}", "https://rare-link.example") for i in range(2)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 2
    assert all(f.page_url is not None for f in result)


def test_evidence_truncates_with_a_count_when_many_pages():
    findings = [_link_finding(f"https://x.example/p{i}", "https://social.example") for i in range(20)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 1
    assert "and 15 more" in result[0].evidence


def test_location_never_embeds_a_run_varying_count():
    """Motivated by app.report._finding_key: the location field must be
    stable across runs (used for cross-run finding identity), so it must
    never bake in a page count that changes as the underlying pattern's
    reach fluctuates run to run."""
    findings_a = [_link_finding(f"https://x.example/p{i}", "https://social.example") for i in range(5)]
    findings_b = [_link_finding(f"https://x.example/p{i}", "https://social.example") for i in range(9)]
    loc_a = aggregate_repetitive_findings(findings_a)[0].location
    loc_b = aggregate_repetitive_findings(findings_b)[0].location
    assert loc_a == loc_b == 'a[href="https://social.example"]'


# --- external_domain_link: aggregate by domain, not exact href -------------
# The real live bug this round found: social-share buttons embed the current
# page's own URL/image/description as query params, so the literal href is
# different on every page even though it's the same share button.

def test_social_share_links_with_per_page_query_params_collapse_by_domain():
    findings = []
    for i in range(11):
        pin = f"https://pinterest.com/pin/create/button/?url=https://x.example/product/{i}&media=img{i}&description=D{i}"
        fb = f"https://www.facebook.com/sharer.php?u=https://x.example/product/{i}"
        x = f"https://x.com/share?url=https://x.example/product/{i}"
        for link in (pin, fb, x):
            findings.append(_social_link_finding(f"https://x.example/product/{i}", link))

    result = aggregate_repetitive_findings(findings)
    # 33 raw findings (11 products x 3 platforms, every href literally
    # distinct) collapse to exactly 3 - one per platform domain - not 33.
    assert len(result) == 3
    domains = {f.location for f in result}
    assert domains == {'a[href*="pinterest.com"]', 'a[href*="www.facebook.com"]', 'a[href*="x.com"]'}
    for f in result:
        assert f.check_id == "external_domain_link"
        assert "11 page(s)" in f.evidence


def test_domain_aggregation_still_below_threshold_leaves_findings_unaggregated():
    findings = [_social_link_finding(f"https://x.example/p{i}", f"https://rare.example/share?id={i}") for i in range(2)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 2


# --- Image checks: no natural shared value - aggregate the whole check_id ---

def test_many_distinct_images_collapse_to_one_check_wide_finding():
    """The real, live-found shape: 81 broken/missing-alt-text images were 81
    almost entirely DISTINCT image URLs, not one repeated - aggregating by
    value would barely help, so these check_ids collapse the whole check_id
    into one finding instead."""
    findings = [_image_finding(f"https://x.example/product{i}", f"https://x.example/img{i}.jpg") for i in range(10)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 1
    agg = result[0]
    assert agg.check_id == "product_image_missing_alt_text"
    assert agg.page_url is None
    assert "10 instance(s)" in agg.evidence
    assert "10 page(s)" in agg.evidence


def test_broken_image_check_id_also_aggregates_by_whole_check():
    findings = [_image_finding(f"https://x.example/p{i}", f"https://x.example/img{i}.jpg", check_id="product_image_broken", title="Product image broken") for i in range(6)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 1
    assert result[0].check_id == "product_image_broken"


def test_check_wide_aggregate_location_is_stable_across_runs():
    a = aggregate_repetitive_findings([_image_finding(f"https://x.example/p{i}", f"https://x.example/img{i}.jpg") for i in range(5)])
    b = aggregate_repetitive_findings([_image_finding(f"https://x.example/p{i}", f"https://x.example/img{i}.jpg") for i in range(9)])
    assert a[0].location == b[0].location


# --- Findings outside the aggregation strategy pass through untouched -------

def test_non_aggregatable_check_id_passes_through_regardless_of_count():
    findings = [Finding(
        check_id="llm_editorial_quality", title="Editorial/professional quality issue found", severity=Severity.MEDIUM,
        confidence=Confidence.CONFIRMED, page_url=f"https://x.example/p{i}", evidence="Lorem ipsum dolor sit amet",
    ) for i in range(10)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 10
    assert all(f.page_url is not None for f in result)


def test_severity_confidence_and_policy_reference_are_preserved_from_the_group():
    findings = [_link_finding(
        f"https://x.example/p{i}", "https://social.example", severity=Severity.MEDIUM,
        confidence=Confidence.POTENTIAL_RISK, policy_reference="GMC: some policy",
    ) for i in range(4)]
    result = aggregate_repetitive_findings(findings)
    assert len(result) == 1
    assert result[0].severity == Severity.MEDIUM
    assert result[0].confidence == Confidence.POTENTIAL_RISK
    assert result[0].policy_reference == "GMC: some policy"


def test_mixed_aggregatable_and_passthrough_findings_all_present():
    findings = (
        [_link_finding(f"https://x.example/p{i}", "https://social.example") for i in range(4)]
        + [Finding(check_id="broken_internal_link", title="Broken internal link (404)", severity=Severity.MEDIUM, confidence=Confidence.CONFIRMED, page_url="https://x.example/dead", evidence="404")]
    )
    result = aggregate_repetitive_findings(findings)
    check_ids = {f.check_id for f in result}
    assert check_ids == {"https_mixed_content_link", "broken_internal_link"}
    assert len(result) == 2


# --- is_aggregatable_check_id ------------------------------------------------

def test_is_aggregatable_check_id():
    assert is_aggregatable_check_id("external_domain_link") is True
    assert is_aggregatable_check_id("product_image_missing_alt_text") is True
    assert is_aggregatable_check_id("llm_editorial_quality") is False
    assert is_aggregatable_check_id("broken_internal_link") is False
