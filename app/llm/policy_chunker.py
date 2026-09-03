"""Splits a scraped GMC Help Center page into citable chunks for the policy
RAG index (Phase C). Chunks at heading boundaries where the page has them
(h1/h2/h3), not arbitrary character counts - each chunk should be a
coherent, citable unit ("the return-window requirement", not half a
sentence). A section's own text is further split only if it's long enough
that stuffing it into one embedding would dilute the vector across multiple
unrelated points - target ~1200 characters (~250-300 tokens), split on
sentence boundaries so a split never lands mid-sentence.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_TARGET_CHUNK_CHARS = 1200
_MIN_CHUNK_CHARS = 40  # drop near-empty fragments (e.g. a lone heading with no body text)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def _split_long_text(text: str, target_chars: int) -> list[str]:
    if len(text) <= target_chars:
        return [text]

    sentences = _SENTENCE_SPLIT_RE.split(text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > target_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_html_by_headings(html: str, target_chars: int = _TARGET_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Returns a list of (section_label, chunk_text) pairs, in document
    order. section_label is the nearest preceding heading's text (or
    "Introduction" for content before the first heading) - this becomes the
    citation label shown alongside the source URL.
    """
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    for tag in main.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    sections: list[tuple[str, list[str]]] = [("Introduction", [])]
    for el in main.find_all(["h1", "h2", "h3", "p", "li"]):
        if el.name in ("h1", "h2", "h3"):
            heading = el.get_text(strip=True)
            if heading:
                sections.append((heading, []))
        else:
            text = el.get_text(strip=True)
            if text:
                sections[-1][1].append(text)

    chunks: list[tuple[str, str]] = []
    for heading, paragraphs in sections:
        combined = _WHITESPACE_RE.sub(" ", " ".join(paragraphs)).strip()
        if len(combined) < _MIN_CHUNK_CHARS:
            continue
        for piece in _split_long_text(combined, target_chars):
            chunks.append((heading, piece))

    return chunks
