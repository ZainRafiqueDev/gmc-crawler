from __future__ import annotations

from pathlib import Path

from app.llm.mock import MockLLMProvider
from app.policy_watcher.fetcher import FixedPolicyPageFetcher
from app.policy_watcher.watcher import InMemoryPolicyHashStore, PolicyWatcher, hash_content

FIXTURES = Path(__file__).parent / "fixtures"
V1 = (FIXTURES / "gmc_policy_v1.html").read_text()
V2 = (FIXTURES / "gmc_policy_v2.html").read_text()
URL = "https://support.google.com/merchants/answer/6149970"

CHANGE_SUMMARY = (
    "GMC added a new requirement: electric scooters and e-bikes must now include "
    "proof of UL 2272 certification in the product data."
)


def test_hash_diff_unchanged_when_content_identical():
    assert hash_content(V1) == hash_content(V1)


def test_hash_diff_detects_change_between_fixture_versions():
    assert hash_content(V1) != hash_content(V2)


async def test_first_check_establishes_baseline_without_firing_on_change():
    fetcher = FixedPolicyPageFetcher({URL: V1})
    fired = []

    async def on_change(result):
        fired.append(result)

    watcher = PolicyWatcher(fetcher, InMemoryPolicyHashStore(), MockLLMProvider(), on_change=on_change)
    result = await watcher.check(URL)

    assert result.changed is True  # nothing stored yet, so it's "new" but not a diff
    assert fired == []


async def test_second_check_same_content_reports_unchanged():
    fetcher = FixedPolicyPageFetcher({URL: V1})
    watcher = PolicyWatcher(fetcher, InMemoryPolicyHashStore(), MockLLMProvider())
    await watcher.check(URL)
    result = await watcher.check(URL)
    assert result.changed is False


async def test_detected_change_extracts_summary_naming_the_actual_change():
    fetcher = FixedPolicyPageFetcher({URL: V1})
    llm = MockLLMProvider(lambda system, user: CHANGE_SUMMARY)
    watcher = PolicyWatcher(fetcher, InMemoryPolicyHashStore(), llm)

    await watcher.check(URL)  # baseline
    fetcher.set_page(URL, V2)
    result = await watcher.check(URL)

    assert result.changed is True
    assert "UL 2272" in result.change_summary


async def test_detected_change_triggers_immediate_full_recheck_not_just_a_log():
    fetcher = FixedPolicyPageFetcher({URL: V1})
    llm = MockLLMProvider(lambda system, user: CHANGE_SUMMARY)
    recheck_calls = []

    async def on_change(result):
        recheck_calls.append(result)

    watcher = PolicyWatcher(fetcher, InMemoryPolicyHashStore(), llm, on_change=on_change)
    await watcher.check(URL)  # baseline, on_change not fired
    fetcher.set_page(URL, V2)
    await watcher.check(URL)

    assert len(recheck_calls) == 1
