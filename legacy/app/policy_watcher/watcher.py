"""Hash-diff + LLM change extraction for GMC policy pages.

On a detected change, `PolicyWatcher.check` fires `on_change` immediately -
it does not wait for the next scheduled recheck. The scheduler wires
`on_change` to a full catalog recheck (see `app.scheduler`).
"""
from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from app.llm.base import LLMProvider, LLMProviderError
from app.policy_watcher.fetcher import PolicyPageFetcher

logger = logging.getLogger("gmc_compliance.policy_watcher")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

CHANGE_EXTRACTION_SYSTEM_PROMPT = """You are monitoring Google Merchant Center policy pages for changes.
You will be given the OLD and NEW text of a policy page. Identify exactly what
changed in policy terms - new requirements, removed requirements, changed
thresholds or wording that affects compliance obligations. Be specific and
name the changed requirement. Respond in 1-3 sentences of plain text, no
markdown, no preamble."""


def extract_text(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def hash_content(html: str) -> str:
    return hashlib.sha256(extract_text(html).encode("utf-8")).hexdigest()


class PolicyHashStore(ABC):
    @abstractmethod
    def get_last(self, url: str) -> tuple[str, str] | None:
        """Returns (hash, raw_html) of the last known version, or None."""
        raise NotImplementedError

    @abstractmethod
    def set_last(self, url: str, content_hash: str, html: str) -> None:
        raise NotImplementedError


class InMemoryPolicyHashStore(PolicyHashStore):
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, str]] = {}

    def get_last(self, url: str) -> tuple[str, str] | None:
        return self._store.get(url)

    def set_last(self, url: str, content_hash: str, html: str) -> None:
        self._store[url] = (content_hash, html)


class PolicyCheckResult(BaseModel):
    url: str
    changed: bool
    change_summary: str | None = None


async def extract_change(old_html: str, new_html: str, llm: LLMProvider) -> str:
    user = f"OLD:\n\"\"\"\n{extract_text(old_html)}\n\"\"\"\n\nNEW:\n\"\"\"\n{extract_text(new_html)}\n\"\"\""
    try:
        return (await llm.complete(system=CHANGE_EXTRACTION_SYSTEM_PROMPT, user=user)).strip()
    except LLMProviderError as exc:
        logger.warning("Policy change extraction LLM call failed: %s", exc)
        return "Policy page changed, but automatic change-summary extraction failed - review manually."


class PolicyWatcher:
    def __init__(
        self,
        fetcher: PolicyPageFetcher,
        hash_store: PolicyHashStore,
        llm_provider: LLMProvider,
        on_change: Callable[[PolicyCheckResult], Awaitable[None]] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._hash_store = hash_store
        self._llm_provider = llm_provider
        self._on_change = on_change

    async def check(self, url: str) -> PolicyCheckResult:
        html = await self._fetcher.fetch(url)
        new_hash = hash_content(html)
        previous = self._hash_store.get_last(url)

        if previous is not None and previous[0] == new_hash:
            logger.info("Policy page unchanged: %s", url)
            return PolicyCheckResult(url=url, changed=False)

        old_html = previous[1] if previous is not None else ""
        summary = None
        if previous is not None:
            summary = await extract_change(old_html, html, self._llm_provider)
            logger.warning("GMC policy change detected at %s: %s", url, summary)

        self._hash_store.set_last(url, new_hash, html)
        result = PolicyCheckResult(url=url, changed=True, change_summary=summary)

        if previous is not None and self._on_change is not None:
            await self._on_change(result)

        return result
