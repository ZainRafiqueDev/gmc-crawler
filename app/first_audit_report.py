"""Renders a FirstAuditRun's FindingRecord rows into the same Markdown
subset app.report.generate_markdown_report already produces, then reuses
app.report_pdf.markdown_to_pdf_bytes verbatim - no new PDF rendering path,
per instruction. Built from the normalized FindingRecord rows + the run's
own snapshot_status, never the old pipeline's findings_json blob (there is
no such blob for a FirstAuditRun - the two pipelines are fully separate).
"""
from __future__ import annotations

from app.db import FindingRecord, FirstAuditRun
from app.findings_builder import ADVISORY_RISK_LEVEL
from app.security.sanitize import sanitize_for_report

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Standing product truth (decisions.md, "Standing truths about the product"):
# every report must state the website-only limit plainly. Printed on EVERY
# report - findings or "No actionable risks found" - right after the header
# and before any verdict, as a one-column table so app.report_pdf renders it
# as a boxed note a reader can't skim past. A clean result here means "the
# website looked clean", never "the Merchant Center account is clean".
WEBSITE_ONLY_LIMIT = (
    "This audit checks the store's public website only. It does NOT check the Merchant Center "
    "product feed or the Merchant Center account, so it cannot see suspension causes that live "
    "there - feed-vs-website price or availability mismatches, GTIN/identifier problems, or "
    "account-level issues (account history, billing, account-level misrepresentation signals). "
    "A clean result here does not mean the account is clean."
)
_SCOPE_LIMIT_BOX = [
    "| Scope limit: website only - not the feed or account |",
    "| --- |",
    f"| {WEBSITE_ONLY_LIMIT} |",
]


def _finding_markdown(finding: FindingRecord) -> list[str]:
    title = sanitize_for_report(finding.title)
    evidence = sanitize_for_report(finding.store_evidence)
    google_rule = sanitize_for_report(finding.google_rule)
    consequence = sanitize_for_report(finding.consequence)
    remediation = sanitize_for_report(finding.remediation)
    source_link = finding.google_source_link  # app.findings_builder already populates this, when a real URL is embedded in the rule's policy_reference

    advisory = finding.risk_level == ADVISORY_RISK_LEVEL
    lines = [f"### {title}"]
    if advisory:
        lines.append("- **Type:** Advisory - not a Google policy violation")
    else:
        lines.append(f"- **Severity:** {finding.severity} | **Risk level:** {finding.risk_level}")
    if finding.page_url:
        lines.append(f"- **Affected URL:** {sanitize_for_report(finding.page_url)}")
    if not advisory:
        lines.append(f"- **Google rule:** {google_rule}")
    lines.append(f"- **What the store shows:** {evidence}")
    lines.append(f"- **Consequence:** {consequence}")
    lines.append(f"- **Recommended fix:** {remediation}")
    if source_link:
        lines.append(f"- **Official source:** {source_link}")
    return lines


def render_first_audit_report_markdown(run: FirstAuditRun, findings: list[FindingRecord]) -> str:
    run_date = run.finished_at or run.started_at
    lines = [
        "# First Audit Report",
        "",
        f"- **Store URL:** {sanitize_for_report(run.url)}",
        f"- **Platform:** {sanitize_for_report(run.platform or 'unknown')}",
        f"- **Status:** {run.snapshot_status or 'UNKNOWN'}",
        f"- **Run date:** {run_date.strftime('%Y-%m-%d %H:%M UTC') if run_date else 'unknown'}",
        f"- **Pages crawled:** {run.pages_crawled if run.pages_crawled is not None else 0}",
    ]
    if run.unreachable_pages_count:
        # Run-level disclosure, independent of any specific finding - a
        # status must never be declared without disclosing how much of the
        # site could actually be read. "Couldn't read it" is not "confirmed
        # clean," and this line makes that visible even when no individual
        # finding happens to cite a blocked page.
        lines.append(
            f"- **Note:** {run.unreachable_pages_count} page(s) could not be read (blocked or unreachable) - "
            "findings about those pages are reported as \"could not verify,\" never a confirmed pass or fail."
        )
    lines.append("")
    lines.extend(_SCOPE_LIMIT_BOX)
    lines.append("")

    risk_findings =[f for f in findings if f.risk_level != ADVISORY_RISK_LEVEL]
    advisories = [f for f in findings if f.risk_level == ADVISORY_RISK_LEVEL]

    if not risk_findings:
        lines.append("## No actionable risks found")
        lines.append("")
        lines.append("This audit did not identify any findings requiring action.")
        lines.append("")
    else:
        lines.append(f"## Findings ({len(risk_findings)})")
        lines.append("")
        for finding in sorted(risk_findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 99)):
            lines.extend(_finding_markdown(finding))
            lines.append("")

    # Kept in their own section, after every risk finding, so an advisory is
    # never read as part of the compliance verdict above it.
    if advisories:
        lines.append(f"## Advisories ({len(advisories)})")
        lines.append("")
        lines.append(
            "Recommendations for customer clarity. These are not Google Merchant Center policy "
            "violations and do not affect the status above."
        )
        lines.append("")
        for finding in advisories:
            lines.extend(_finding_markdown(finding))
            lines.append("")

    return "\n".join(lines).rstrip("\n")
