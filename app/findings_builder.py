"""Converts app.db.Evaluation rows into app.db.FindingRecord rows (work-order
step 4, piece 4, part 3) and rolls the result into one compliance-snapshot
status for the FirstAuditRun.

A Finding is only ever emitted when the spec's four required parts are all
present: Google rule (rule.policy_reference), evidence (rendered from the
Evaluation's own evidence_json - both sides, never just "conflict"), risk
level, and exact remediation (rule.remediation, or - for an existing_check-
delegated evaluation - that check's own more specific recommended_fix). No
Evaluation with a missing policy_reference/remediation can reach this
function in the first place, since Rule requires both fields at load time
(app.rules.schema) - there is no silent-degrade path here, only "the rule
author didn't fill this in," which fails loudly at YAML load time instead.

risk_level and consequence are both derived from the finding's own severity
(_RISK_LEVEL_BY_SEVERITY / _CONSEQUENCE_BY_SEVERITY below), never from the
parent rule's impact tier - a rule's impact is calibrated for its single
worst case, but an existing_check-delegated rule can emit findings across a
whole range of its own severities, and inheriting one fixed impact tier
produced real internally-inconsistent findings (risk_level="suspension_risk"
on a severity="medium" finding). Keying both off severity instead makes them
structurally unable to disagree, for any rule.

A "pass" Evaluation never becomes a Finding. A "needs_review" Evaluation
DOES become a Finding (the same CANNOT_VERIFY-style discipline used
elsewhere in this project - surfaced, not suppressed), with wording
softened to state uncertainty rather than asserting the conflict as fact.

The one exception to "risk_level comes from severity": a rule whose impact
is quality_improvement is an ADVISORY - it has no Google source behind it
(app.rules.loader forbids it a policy_reference), so its findings get
risk_level="advisory", no google_rule, consequence wording that explicitly
disclaims a Google violation, and are excluded from compute_snapshot_status.
That is a categorical "is this a Google claim at all" property of the rule,
not a per-finding severity grade, so it is keyed off the rule.
"""
from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Evaluation, FindingRecord
from app.models import ImpactTier, Severity
from app.rules.schema import ConditionType, Rule

# Consequence (and risk_level) are a direct function of the FINDING's own
# severity - never of the parent rule's impact tier. Fixed live: a rule's
# `impact` is calibrated for that rule's own worst case (e.g.
# business_identity_conflict's existing_check can emit anything from a
# CRITICAL "no contact info at all" finding down to a MEDIUM "two different
# phone numbers" one), but every evaluation that rule produces inherited the
# SAME impact tier regardless of its own severity - a real finding showed
# risk_level/consequence="suspension_risk" on a severity="medium" finding,
# which reads as internally inconsistent and undermines trust in the
# grading. Keying both fields off severity instead makes them structurally
# unable to disagree with each other, for every rule, not just this one.
#
# Wording stays within spec section 5's constraint (phrased as a
# possibility, never "Google will suspend you").
_RISK_LEVEL_BY_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "suspension_risk",
    Severity.HIGH: "serious_risk",
    Severity.MEDIUM: "listing_disapproval",
    Severity.LOW: "minor_issue",
}
_CONSEQUENCE_BY_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "This may result in potential suspension risk for the whole Merchant Center account.",
    Severity.HIGH: "This may result in serious risk to the account or affected product listings, and could escalate toward suspension if left unresolved.",
    Severity.MEDIUM: "This may result in likely disapproval of the affected product listing(s), or limited visibility/eligibility - not itself a suspension-level issue.",
    Severity.LOW: "This is a minor issue - addressing it is recommended, but it is not tied to a known enforcement action.",
}

ADVISORY_RISK_LEVEL = "advisory"
_ADVISORY_CONSEQUENCE = (
    "This is not a Google Merchant Center policy violation and does not affect the compliance status - "
    "it is a recommendation for customer clarity."
)

_SEVERITY_TO_SNAPSHOT_STATUS: dict[Severity, str] = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "AT_RISK",
    Severity.MEDIUM: "ACTION_REQUIRED",
    Severity.LOW: "ACTION_REQUIRED",
}


def _render_comparison_evidence(evidence: dict) -> str:
    """cross_page_contradiction / numeric_mismatch: both sides, concretely -
    "returns page says 30, FAQ says 14", not just "conflict"."""
    parts = []
    for obs in evidence.get("observations", []):
        label = obs.get("resource_type") or obs.get("method") or "unknown source"
        parts.append(f'{label} shows "{obs["value"]}" ({obs["source_url"]}, {obs["method"]}, confidence {obs["confidence"]:.2f})')
    return "; ".join(parts) if parts else "No comparable evidence recorded."


def _render_missing_resource_evidence(evidence: dict) -> str:
    if "reason" in evidence:
        return f"Could not confirm either way - {evidence['reason']}."
    if "soft_404_flagged_pages" in evidence:
        pages = ", ".join(evidence["soft_404_flagged_pages"])
        return f"A page was found but could not be confirmed as genuine, distinct content: {pages}."
    if "blocked_or_unreachable_pages" in evidence:
        pages = "; ".join(
            f"{p['url']} ({p['failure_category'] or 'unreachable'})" for p in evidence["blocked_or_unreachable_pages"]
        )
        return (
            f"A likely {evidence.get('resource_type', 'required')} page was found but could not be read: {pages}. "
            "This is not a confirmed absence - the page could not be verified, not shown to be missing."
        )
    return f"No reachable {evidence.get('resource_type', 'required')} page was found anywhere on the site."


def _render_all_missing_evidence(evidence: dict) -> str:
    if "reason" in evidence:
        return f"Could not confirm either way - {evidence['reason']}."
    fields = ", ".join(evidence.get("fields_checked", []))
    return f"None of the following were found anywhere on the site: {fields}."


def _render_evidence(rule: Rule, evaluation: Evaluation) -> str:
    evidence = json.loads(evaluation.evidence_json)
    condition_type = rule.condition.type
    if condition_type in (ConditionType.CROSS_PAGE_CONTRADICTION, ConditionType.NUMERIC_MISMATCH):
        return _render_comparison_evidence(evidence)
    if condition_type == ConditionType.MISSING_RESOURCE:
        return _render_missing_resource_evidence(evidence)
    if condition_type == ConditionType.ALL_MISSING:
        return _render_all_missing_evidence(evidence)
    return json.dumps(evidence)  # existing_check never reaches here - handled separately below


def _primary_page_url(evaluation: Evaluation, evidence: dict) -> str | None:
    observations = evidence.get("observations")
    if observations:
        return observations[0].get("source_url")
    blocked_pages = evidence.get("blocked_or_unreachable_pages")
    if blocked_pages:
        return blocked_pages[0].get("url")
    return None


def _finding_from_delegated_evaluation(rule: Rule, evaluation: Evaluation, evidence: dict) -> FindingRecord:
    """existing_check: the delegated Finding already has its own title/
    evidence/severity/policy_reference/recommended_fix - use those directly
    rather than the rule's own generic fields, which are a fallback only."""
    delegated = evidence["delegated_finding"]
    severity = Severity(delegated["severity"])
    needs_review_suffix = " (could not be confirmed with full confidence)" if evaluation.result == "needs_review" else ""
    google_rule = delegated.get("policy_reference") or rule.policy_reference
    return FindingRecord(
        audit_run_id=evaluation.audit_run_id, evaluation_id=evaluation.id,
        check_id=delegated["check_id"], title=delegated["title"] + needs_review_suffix,
        severity=delegated["severity"], risk_level=_RISK_LEVEL_BY_SEVERITY[severity],
        page_url=delegated.get("page_url"), store_evidence=delegated["evidence"],
        google_rule=google_rule,
        consequence=_CONSEQUENCE_BY_SEVERITY[severity],
        remediation=delegated.get("recommended_fix") or rule.remediation,
        google_source_link=rule.google_source_url,
    )


def _finding_from_rule_evaluation(rule: Rule, evaluation: Evaluation) -> FindingRecord:
    evidence = json.loads(evaluation.evidence_json)
    needs_review_suffix = " (could not be confirmed with full confidence)" if evaluation.result == "needs_review" else ""
    advisory = rule.impact == ImpactTier.QUALITY_IMPROVEMENT
    return FindingRecord(
        audit_run_id=evaluation.audit_run_id, evaluation_id=evaluation.id,
        check_id=rule.id, title=rule.title + needs_review_suffix,
        severity=rule.severity.value,
        risk_level=ADVISORY_RISK_LEVEL if advisory else _RISK_LEVEL_BY_SEVERITY[rule.severity],
        page_url=_primary_page_url(evaluation, evidence), store_evidence=_render_evidence(rule, evaluation),
        google_rule=None if advisory else rule.policy_reference,
        consequence=_ADVISORY_CONSEQUENCE if advisory else _CONSEQUENCE_BY_SEVERITY[rule.severity],
        remediation=rule.remediation,
        google_source_link=rule.google_source_url,
    )


def build_finding(rule: Rule, evaluation: Evaluation) -> FindingRecord | None:
    if evaluation.result == "pass":
        return None

    evidence = json.loads(evaluation.evidence_json)
    if "delegated_finding" in evidence:
        return _finding_from_delegated_evaluation(rule, evaluation, evidence)
    return _finding_from_rule_evaluation(rule, evaluation)


async def build_findings_for_run(session: AsyncSession, rules_by_id: dict[str, Rule], evaluations: list[Evaluation]) -> list[FindingRecord]:
    records: list[FindingRecord] = []
    for evaluation in evaluations:
        rule = rules_by_id.get(evaluation.rule_id)
        if rule is None:
            continue  # defensive - every Evaluation here was produced from a currently-loaded rule
        record = build_finding(rule, evaluation)
        if record is not None:
            session.add(record)
            records.append(record)

    await session.flush()
    return records


def compute_snapshot_status(findings: list[FindingRecord]) -> str:
    """COMPLIANT only when there are zero risk findings - otherwise the
    single highest-severity risk finding present decides the status, in the
    fixed CRITICAL > AT_RISK > ACTION_REQUIRED order. Advisory findings
    (risk_level="advisory") never count: they make no Google-violation claim.
    """
    risk_findings = [f for f in findings if getattr(f, "risk_level", None) != ADVISORY_RISK_LEVEL]
    if not risk_findings:
        return "COMPLIANT"

    statuses = {_SEVERITY_TO_SNAPSHOT_STATUS[Severity(f.severity)] for f in risk_findings}
    for status in ("CRITICAL", "AT_RISK", "ACTION_REQUIRED"):
        if status in statuses:
            return status
    return "COMPLIANT"
