"""Content+DOM hashing for the cheap change-detection check (Goal 2.1). Two
hashes per page because they catch different kinds of drift:

- content_hash: normalized visible text. Catches copy changes (new price,
  edited policy wording) but ignores whitespace/formatting noise.
- dom_hash: the tag-structure skeleton (tag names in document order,
  ignoring text/attributes). Catches structural/template changes (a new
  section added, a broken template) even when visible text is unchanged.
"""
from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup


def compute_content_hash(text: str | None) -> str:
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_dom_hash(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    structure = "|".join(tag.name for tag in soup.find_all(True))
    return hashlib.sha256(structure.encode("utf-8")).hexdigest()
