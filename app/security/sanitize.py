"""Sanitizes text pulled from a (possibly malicious or compromised) target
site before it's embedded into a generated report. Every Finding field that
can contain content lifted from the audited page - title, evidence,
location, policy_reference, recommended_fix - is untrusted input: escaping
it here means a script/style-injection payload sitting on the audited site
can never execute once the report is rendered as HTML (e.g. by the
frontend's Markdown viewer), regardless of how that renderer is configured.

Applied at the report-rendering boundary (app/report.py), not at crawl/
extraction time - check logic (regex matching, LLM grounding) needs the
original, unescaped text to work correctly; only the final rendered output
needs to be safe.
"""
from __future__ import annotations

import html
import re

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DEFAULT_MAX_LENGTH = 2000


def sanitize_for_report(text: str | None, max_length: int = _DEFAULT_MAX_LENGTH) -> str:
    if not text:
        return ""
    text = _CONTROL_CHAR_RE.sub("", text)
    text = html.escape(text, quote=False)
    if len(text) > max_length:
        text = text[:max_length] + "... [truncated]"
    return text
