"""Basic Markdown -> PDF export. Mirrors app/report_docx.py exactly - same
Markdown subset (#/##/### headers, "- " bullets, "**bold**" leading labels,
plain paragraphs), same HTML-entity-unescaping fix, same typography (a
single sans-serif family, consistent heading sizes, severity color coding)
and same output shape - just a different library (reportlab, pure Python,
no native Pango/cairo dependency to install on top of what this project
already needs). Helvetica is reportlab's built-in font metrically
compatible with Arial (same glyph widths), so no font embedding is needed
to get an Arial-equivalent sans-serif look everywhere.
"""
from __future__ import annotations

import html
import io
import re
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_BOLD_LEADING_RE = re.compile(r"^\*\*(.+?)\*\*(.*)$")
_SEVERITY_TAG_RE = re.compile(r"^(\[(CRITICAL|HIGH|MEDIUM|LOW)\])(.*)$")
_SEVERITY_FIELD_RE = re.compile(r"^(Severity:\s*)(critical|high|medium|low)(.*)$", re.IGNORECASE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
# See app/report_docx.py's identical constant - the one image shape
# app/report.py ever emits (annotated screenshots follow-up, Part 3).
_SCREENSHOT_LINE_RE = re.compile(r"^-\s*\*\*Screenshot:\*\*\s*!\[([^\]]*)\]\(([^)]+)\)\s*$")
_MAX_IMAGE_WIDTH_IN = 5.5

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_INK = HexColor("#1E1E2E")
_BRAND = HexColor("#4F46E5")
_RULE = HexColor("#D9D9E3")

_SEVERITY_COLOR = {
    "critical": "#B91C1C",
    "high": "#C2410C",
    "medium": "#A16207",
    "low": "#475569",
}

_styles = getSampleStyleSheet()
_STYLE_H1 = ParagraphStyle(
    "ReportH1", parent=_styles["Heading1"], fontName=_FONT_BOLD, fontSize=22, leading=26,
    textColor=_BRAND, spaceBefore=4, spaceAfter=10,
)
_STYLE_H2 = ParagraphStyle(
    "ReportH2", parent=_styles["Heading2"], fontName=_FONT_BOLD, fontSize=16, leading=20,
    textColor=_INK, spaceBefore=16, spaceAfter=4,
)
_STYLE_H3 = ParagraphStyle(
    "ReportH3", parent=_styles["Heading3"], fontName=_FONT_BOLD, fontSize=13, leading=17,
    textColor=_INK, spaceBefore=12, spaceAfter=6,
)
_STYLE_BODY = ParagraphStyle(
    "ReportBody", parent=_styles["Normal"], fontName=_FONT, fontSize=11, leading=15.5, textColor=_INK,
)
_STYLE_BULLET = ParagraphStyle("ReportBullet", parent=_STYLE_BODY, leftIndent=0.28 * inch, bulletIndent=0.1 * inch)
_STYLE_TABLE_CELL = ParagraphStyle("ReportTableCell", parent=_STYLE_BODY, fontSize=9.5, leading=12.5)
_STYLE_TABLE_HEADER_CELL = ParagraphStyle("ReportTableHeaderCell", parent=_STYLE_TABLE_CELL, fontName=_FONT_BOLD, textColor=colors.white)


def _escape_for_reportlab(text: str) -> str:
    """reportlab's Paragraph interprets a small XML-like markup language in
    its input string (the same entity scheme as HTML) - raw &/</> must be
    escaped or it either mis-renders or raises a parse error. Order matters:
    escape & first, or the &amp; this produces for a literal < would itself
    get re-escaped.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _font_span(text: str, color: str, *, bold: bool = False) -> str:
    escaped = _escape_for_reportlab(text)
    if bold:
        escaped = f"<b>{escaped}</b>"
    return f'<font color="{color}">{escaped}</font>'


def _severity_aware_markup(text: str) -> str:
    """Colors a leading "[CRITICAL]"/"[HIGH]"/... tag (page-by-page compact
    findings) or a "Severity: <level>" field (full finding detail lines) to
    match the severity color coding used in the web report and .docx export
    - everything else in the line renders as plain escaped text.
    """
    tag_match = _SEVERITY_TAG_RE.match(text)
    if tag_match:
        level = tag_match.group(2).lower()
        rest = tag_match.group(3)
        tag_markup = _font_span(tag_match.group(1), _SEVERITY_COLOR[level], bold=True)
        return f"{tag_markup} {_line_to_markup(rest.lstrip())}" if rest else tag_markup

    field_match = _SEVERITY_FIELD_RE.match(text)
    if field_match:
        level = field_match.group(2).lower()
        level_markup = _font_span(field_match.group(2), _SEVERITY_COLOR[level], bold=True)
        rest = field_match.group(3)
        return f"{_escape_for_reportlab(field_match.group(1))}{level_markup}{_line_to_markup(rest) if rest else ''}"

    return _line_to_markup(text)


def _line_to_markup(text: str) -> str:
    """Converts one already-html.unescape()'d line into reportlab Paragraph
    markup: escape everything for reportlab's parser, except a **bold**
    leading span (app/report.py's finding-title format), which becomes a
    real <b> tag - handled the same way app/report_docx.py's _add_runs does.
    """
    match = _BOLD_LEADING_RE.match(text)
    if match:
        bold_part, rest = match.group(1), match.group(2)
        return f"<b>{_escape_for_reportlab(bold_part)}</b>{_escape_for_reportlab(rest)}"
    return _escape_for_reportlab(text)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _build_table(header: list[str], rows: list[list[str]]) -> Table:
    # app/report.py's Policy-by-Policy Review matrix is the only markdown
    # table this project generates - handled explicitly rather than as
    # general GFM table support, same scope discipline as the rest of this
    # module (a specific known shape, not a general Markdown parser).
    header_row = [Paragraph(_escape_for_reportlab(c), _STYLE_TABLE_HEADER_CELL) for c in header]
    body_rows = [[Paragraph(_escape_for_reportlab(c), _STYLE_TABLE_CELL) for c in row] for row in rows]
    table = Table([header_row, *body_rows], colWidths=None, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("GRID", (0, 0), (-1, -1), 0.5, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#F7F7FB")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _build_screenshot_flowables(base_dir: Path | None, rel_path: str) -> list:
    flowables = [Paragraph("<b>Screenshot</b>", _STYLE_BODY)]
    full_path = (base_dir / rel_path) if base_dir is not None else None
    if full_path is not None and full_path.is_file():
        try:
            with PILImage.open(full_path) as im:
                width_px, height_px = im.size
            width = _MAX_IMAGE_WIDTH_IN * inch
            height = width * (height_px / width_px)
            flowables.append(RLImage(str(full_path), width=width, height=height))
            flowables.append(Spacer(1, 6))
            return flowables
        except Exception:  # noqa: BLE001 - a corrupt/unreadable image degrades to a text fallback, never breaks the export
            pass
    flowables.append(Paragraph(_escape_for_reportlab(f"(image not available - expected at {rel_path})"), _STYLE_BODY))
    flowables.append(Spacer(1, 6))
    return flowables


def markdown_to_pdf_bytes(markdown_text: str, base_dir: Path | str | None = None) -> bytes:
    base_dir = Path(base_dir) if base_dir is not None else None
    doc = SimpleDocTemplate(
        io.BytesIO(), pagesize=LETTER,
        leftMargin=1 * inch, rightMargin=1 * inch, topMargin=1 * inch, bottomMargin=1 * inch,
        title="GMC Compliance Audit Report",
    )
    flowables = []

    # See app/report_docx.py for why this unescape happens first: fields
    # sanitized for the Markdown/HTML rendering path (& -> &amp; etc.) need
    # decoding back to real characters before this module applies its own
    # escaping for reportlab's markup - otherwise a literal "&" would show
    # up as the literal text "&amp;" in the PDF.
    lines = [html.unescape(raw_line).rstrip() for raw_line in markdown_text.splitlines()]

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if (
            line.lstrip().startswith("|") and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1].strip())
        ):
            header = _split_table_row(line)
            body_rows = []
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                body_rows.append(_split_table_row(lines[j]))
                j += 1
            flowables.append(_build_table(header, body_rows))
            flowables.append(Spacer(1, 8))
            i = j
            continue

        if line.startswith("### "):
            flowables.append(Paragraph(_escape_for_reportlab(line[4:].strip()), _STYLE_H3))
        elif line.startswith("## "):
            flowables.append(Paragraph(_escape_for_reportlab(line[3:].strip()), _STYLE_H2))
            flowables.append(HRFlowable(width="100%", thickness=0.75, color=_RULE, spaceBefore=0, spaceAfter=8))
        elif line.startswith("# "):
            flowables.append(Paragraph(_escape_for_reportlab(line[2:].strip()), _STYLE_H1))
        elif _SCREENSHOT_LINE_RE.match(line.strip()):
            match = _SCREENSHOT_LINE_RE.match(line.strip())
            flowables.extend(_build_screenshot_flowables(base_dir, match.group(2)))
        elif line.lstrip().startswith("- "):
            text = line.lstrip()[2:]
            flowables.append(Paragraph(_severity_aware_markup(text), _STYLE_BULLET, bulletText="•"))
            flowables.append(Spacer(1, 3))
        else:
            flowables.append(Paragraph(_severity_aware_markup(line.strip()), _STYLE_BODY))
            flowables.append(Spacer(1, 5))
        i += 1

    doc.build(flowables)
    return doc.filename.getvalue()
