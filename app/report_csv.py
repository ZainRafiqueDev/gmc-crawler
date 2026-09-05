"""Full-detail CSV export (report-bloat follow-up round, Part 3): the
escape hatch for someone who genuinely needs every individual finding
instance at full granularity (e.g. a developer fixing every broken image
one by one) - the Markdown/docx/PDF reports now render the aggregated view
(app.finding_aggregation), which is the right default for a human reading
the report end to end, but must never make the raw per-instance data harder
to get for someone who needs it. This operates directly on the
finding *objects* (never the pre-aggregated ones - callers pass the same
raw findings list the check pipeline produced, before any aggregation),
not on rendered Markdown - a CSV needs the structured fields, not prose.
"""
from __future__ import annotations

import csv
import io

from app.models import Finding

_COLUMNS = [
    "check_id", "title", "severity", "confidence", "page_url", "evidence",
    "location", "policy_reference", "recommended_fix", "impact_tier",
    "ads_eligibility_impact", "verification_method", "evidence_verified",
    "detected_at", "screenshot_path",
]


def _row(f: Finding) -> list[str]:
    return [
        f.check_id,
        f.title,
        f.severity.value,
        f.confidence.value,
        f.page_url or "",
        f.evidence,
        f.location or "",
        f.policy_reference or "",
        f.recommended_fix or "",
        f.impact_tier.value,
        f.ads_eligibility_impact.value,
        f.verification_method.value,
        str(f.evidence_verified),
        f.detected_at.isoformat(),
        f.screenshot_path or "",
    ]


def findings_to_csv_bytes(findings: list[Finding]) -> bytes:
    """One row per raw finding instance, in the order given - pass the
    findings list exactly as the check pipeline produced it (unaggregated)
    to get the full per-instance count; passing an already-aggregated list
    just produces a (much shorter) CSV of the aggregated rows instead, which
    is a legitimate thing to want but not what this export exists for.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(_COLUMNS)
    for f in findings:
        writer.writerow(_row(f))
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 correctly without a manual import step
