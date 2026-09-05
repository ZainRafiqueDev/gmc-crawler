"""Aggregates repetitive per-instance findings into one finding per distinct
underlying issue (report-bloat follow-up round). Generalizes the pattern
app.checks.business_identity.check_business_identity_consistency already
uses for a phone-number/email inconsistency (one Finding per distinct value,
with a page list in its evidence) rather than inventing a new mechanism.

Found live: a real audit against britanniagifts.us produced 6,337 findings
- nearly all of it a handful of patterns (external-domain social-share
links, hardcoded http:// nav links, missing image alt text, broken images)
logged as one fully-detailed, separate Finding per page or per image
instance. The underlying checks (app/checks/deterministic.py,
app/checks/product_images.py) are unchanged - they still discover every
real instance; this module only changes how many Finding objects the
*report* renders for a set of instances that are structurally the same
issue repeated across the site.

Deliberately a post-hoc pass over an already-built findings list (same
shape as app.impact_tier.apply_impact_tiers/app.ads_eligibility.
apply_ads_eligibility_impact - both already run after check functions
produce their raw findings), not a change to any check function itself:
callers that need the full, unaggregated per-instance list (a CSV export,
audit-history delta comparison) use the findings list exactly as the
checks produced it; only report rendering (app.report.generate_markdown_report/
generate_delta_report) asks for the aggregated view.
"""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlparse

from app.models import Finding

# check_id -> the CSS-selector-embedded value to aggregate by (the specific
# link/image URL - each one is its own distinct, worth-naming fact, e.g.
# "this exact link/image appears on N pages"), or None to collapse EVERY
# instance of that check_id into a single finding (no natural shared value
# to group by - "images missing alt text" is one systemic pattern across
# many different images, not N copies of the same fact).
_HREF_RE = re.compile(r'href="([^"]*)"')
_SRC_RE = re.compile(r'src="([^"]*)"')

_BY_HREF = "href"
_BY_DOMAIN = "domain"
_BY_SRC = "src"
_BY_CHECK = "check"

_AGGREGATION_STRATEGY: dict[str, str] = {
    # external_domain_link: found live to usually be a social-share button
    # (Pinterest/Facebook/X) whose href embeds THIS page's own URL/image/
    # description as query parameters - literally a different string on
    # every single page even though it's the exact same share button
    # everywhere (confirmed live: 31 "external-domain link" findings were
    # 33 distinct literal hrefs, none repeated - aggregating by exact href
    # would have changed nothing). The domain is the real, meaningful
    # aggregation key here - "a Pinterest share button" is one fact
    # regardless of which product's URL happens to be embedded in it.
    "external_domain_link": _BY_DOMAIN,
    # https_mixed_content_link: found live to be the opposite shape - a
    # genuinely identical hardcoded URL (no query-string variation)
    # repeated verbatim across hundreds of pages (296 -> 2 distinct exact
    # links) - the exact-href match already collapses this correctly, and
    # a domain-only key would over-merge two different hardcoded links
    # that happen to share a domain into one misleading finding.
    "https_mixed_content_link": _BY_HREF,
    # Images: found live to be the opposite shape - 81 "broken image"
    # findings were 81 almost entirely DISTINCT image URLs (mostly
    # appearing once or twice each, not one image repeated site-wide), so
    # aggregating by image value would barely reduce anything (each value's
    # own group stays below _MIN_INSTANCES_TO_AGGREGATE). The real pattern
    # here isn't "the same image repeated" - it's "this general problem
    # (broken / missing alt text / etc.) recurs broadly across the
    # catalog", a systemic fact best represented as one finding with a
    # representative sample, matching the spec's own phrasing ("found on
    # 287 of 305 product pages, including: [5 examples]").
    "broken_image": _BY_CHECK,
    "product_image_broken": _BY_CHECK,
    "product_image_missing_alt_text": _BY_CHECK,
    "product_image_placeholder_filename": _BY_CHECK,
    "product_image_low_resolution": _BY_CHECK,
}

# Below this many instances, aggregating buys nothing but an extra layer of
# indirection over just reading the findings directly - a check_id with only
# a couple of hits stays exactly as informative rendered individually.
_MIN_INSTANCES_TO_AGGREGATE = 3
_MAX_EXAMPLES_IN_EVIDENCE = 5


def _selector_value(location: str | None, pattern: re.Pattern[str]) -> str | None:
    if not location:
        return None
    m = pattern.search(location)
    return m.group(1) if m else None


def _domain_value(location: str | None) -> str | None:
    href = _selector_value(location, _HREF_RE)
    if not href:
        return None
    netloc = urlparse(href).netloc
    return netloc or None


def _format_examples(items: list[str], max_examples: int = _MAX_EXAMPLES_IN_EVIDENCE) -> str:
    """items may contain duplicates (e.g. the same page appearing twice for
    two different reasons) - de-duplicated here, order-preserving, so the
    example list itself never repeats an entry."""
    unique = list(dict.fromkeys(items))
    if len(unique) <= max_examples:
        return ", ".join(unique)
    shown = ", ".join(unique[:max_examples])
    return f"{shown}, and {len(unique) - max_examples} more"


def _aggregate_group(check_id: str, group: list[Finding], group_key: str | None) -> Finding:
    """One representative Finding for `group` - same check_id/title/severity/
    confidence/policy_reference/recommended_fix/verification_method as the
    group's own findings (the underlying judgment is untouched, only how
    many Finding objects represent it), with evidence/location rebuilt to
    name every distinct page/value the group spans rather than repeating
    one Finding per occurrence.
    """
    representative = group[0]
    pages = [f.page_url for f in group if f.page_url]
    strategy = _AGGREGATION_STRATEGY.get(check_id)

    if strategy == _BY_DOMAIN and group_key:
        distinct_pages = len(dict.fromkeys(pages))
        evidence = f"Link(s) to {group_key} found on {distinct_pages} page(s): {_format_examples(pages)}"
        # 'contains' selector, not an exact-match one - honestly reflects
        # that this represents multiple distinct hrefs sharing a domain
        # (see _AGGREGATION_STRATEGY's comment on why exact-href matching
        # doesn't work for this check_id), while staying just as stable
        # across runs (the domain itself doesn't change run to run).
        location = f'a[href*="{group_key}"]'
    elif strategy in (_BY_HREF, _BY_SRC) and group_key:
        kind = "Link to" if strategy == _BY_HREF else "Image"
        evidence = f"{kind} {group_key} found on {len(pages)} page(s): {_format_examples(pages)}"
        # Location keeps the exact same shape a single, non-aggregated
        # finding for this check_id already uses (e.g. 'a[href="..."]') -
        # deliberately WITHOUT the page count, which changes every run even
        # when the underlying issue doesn't. app.report._finding_key uses
        # this field for cross-run identity (Finding.evidence is allowed to
        # change without counting as a "changed" finding) - embedding a
        # run-varying count here would make that identity unstable for
        # exactly the findings this module creates.
        selector = "href" if strategy == _BY_HREF else "src"
        location = f'{"a" if strategy == _BY_HREF else "img"}[{selector}="{group_key}"]'
    else:
        # _BY_CHECK: no single shared value - name the pattern itself, with
        # each instance's own value/page as an example pair.
        pairs = [
            f'{_selector_value(f.location, _SRC_RE) or f.location or "?"} (on {f.page_url or "unknown page"})'
            for f in group
        ]
        distinct_pages = len(dict.fromkeys(pages))
        evidence = (
            f"{representative.title} - {len(group)} instance(s) across {distinct_pages} page(s), "
            f"including: {_format_examples(pairs)}"
        )
        # Stable per check_id (only one _BY_CHECK aggregate ever exists per
        # check_id - see aggregate_repetitive_findings) - same
        # run-varying-count-free reasoning as above.
        location = "aggregated site-wide (see evidence for exact pages/images)"

    return representative.model_copy(update={
        "page_url": None,
        "evidence": evidence,
        "location": location,
    })


def is_aggregatable_check_id(check_id: str) -> bool:
    """Whether this check_id is ever affected by aggregate_repetitive_findings
    - used by app.report._finding_key to know when it's safe to fold
    `location` into cross-run finding identity. Safe only for these
    check_ids: their location is always a fixed, code-generated CSS
    selector (never model-generated free text that could drift in wording
    between two runs of the same underlying finding, the way an LLM-graded
    check's location can)."""
    return check_id in _AGGREGATION_STRATEGY


def aggregate_repetitive_findings(findings: list[Finding]) -> list[Finding]:
    """Returns a new list: every finding whose check_id isn't in
    _AGGREGATION_STRATEGY passes through unchanged; findings that are,
    collapse into one aggregated Finding per distinct group (by link/image
    value, or the whole check_id when there's no natural shared value) -
    but only once a check_id has enough raw instances that aggregating
    actually helps (_MIN_INSTANCES_TO_AGGREGATE) - a check_id that only
    fired once or twice is left exactly as-is.
    """
    by_check: dict[str, list[Finding]] = defaultdict(list)
    passthrough: list[Finding] = []
    for f in findings:
        if f.check_id in _AGGREGATION_STRATEGY:
            by_check[f.check_id].append(f)
        else:
            passthrough.append(f)

    aggregated: list[Finding] = []
    for check_id, group in by_check.items():
        if len(group) < _MIN_INSTANCES_TO_AGGREGATE:
            passthrough.extend(group)
            continue

        strategy = _AGGREGATION_STRATEGY[check_id]
        if strategy == _BY_CHECK:
            aggregated.append(_aggregate_group(check_id, group, None))
            continue

        by_value: dict[str, list[Finding]] = defaultdict(list)
        no_value: list[Finding] = []
        for f in group:
            if strategy == _BY_DOMAIN:
                value = _domain_value(f.location)
            else:
                pattern = _HREF_RE if strategy == _BY_HREF else _SRC_RE
                value = _selector_value(f.location, pattern)
            if value:
                by_value[value].append(f)
            else:
                no_value.append(f)

        for value, value_group in by_value.items():
            if len(value_group) < _MIN_INSTANCES_TO_AGGREGATE:
                passthrough.extend(value_group)
            else:
                aggregated.append(_aggregate_group(check_id, value_group, value))
        # A finding of this check_id whose location didn't carry a parseable
        # value (shouldn't normally happen - every check_id in
        # _AGGREGATION_STRATEGY always sets one - but degrades to
        # passthrough rather than silently dropping it if it ever did).
        passthrough.extend(no_value)

    return passthrough + aggregated
