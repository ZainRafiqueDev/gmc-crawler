#!/usr/bin/env python
"""Runs the real audit pipeline against the 5 stores in validation_set.json
and scores the tool's findings against human-determined ground truth, per
category, as precision/recall - not a single blended accuracy percentage.

    python -m validation.score_validation
    python -m validation.score_validation --results-dir validation/results

This reuses the exact same pipeline as audit.py (app.graph.run_audit) - no
separate/simplified re-implementation of the audit - so what's being scored
is genuinely what a user would get from the CLI or the web app.

Ground truth in validation_set.json must be filled in by hand (see
validation_set.template.json for the format and instructions); this script
never generates or guesses ground truth itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import async_playwright

from app.config import load_settings
from app.db import Database
from app.graph import run_audit
from app.llm.cache import LLMCache
from app.models import Confidence, Finding
from app.security.ssrf_guard import SSRFBlockedError, assert_public_url
from validation.category_mapping import CATEGORIES, category_for_check_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("gmc_audit.validation")

VALID_VERDICTS = {"pass", "fail", "not_applicable", "unknown"}


@dataclass
class CategoryTally:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    excluded: int = 0  # not_applicable / unknown ground truth, or tool said cannot_verify
    disagreements: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None


def tool_verdict_for_category(findings: list[Finding], category: str) -> str:
    """'fail' if the tool raised a confirmed or potential_risk finding in this
    category, 'cannot_verify' if every finding in it was cannot_verify (e.g.
    no ANTHROPIC/OPENAI key configured), else 'pass' (no findings at all)."""
    relevant = [f for f in findings if category_for_check_id(f.check_id) == category]
    if not relevant:
        return "pass"
    if any(f.confidence != Confidence.CANNOT_VERIFY for f in relevant):
        return "fail"
    return "cannot_verify"


async def run_one_store(url: str, settings) -> list[Finding]:
    await assert_public_url(url if "://" in url else f"https://{url}")
    db = Database(settings.database_url)
    await db.init()
    llm_cache = LLMCache(db)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            result = await asyncio.wait_for(
                run_audit(url, settings, browser, llm_cache, db), timeout=settings.audit_timeout_seconds,
            )
        finally:
            await browser.close()
            await db.dispose()
    return result.get("findings", [])


def load_validation_set(path: Path) -> list[dict]:
    if not path.exists():
        print(
            f"Error: {path} not found.\n"
            f"Copy validation/validation_set.template.json to validation/validation_set.json, "
            f"fill in 5 real store URLs and your own manual ground-truth verdicts, then re-run."
        )
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    stores = data["stores"]
    for store in stores:
        if "REPLACE-ME" in store["url"]:
            print(f"Error: {path} still has a placeholder URL ({store['url']!r}). Fill in real store URLs first.")
            sys.exit(1)
    return stores


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-set", default="validation/validation_set.json", help="Path to the filled-in ground-truth file.")
    parser.add_argument("--results-dir", default="validation/results", help="Where to write raw per-store findings JSON, for later re-scoring without re-crawling.")
    args = parser.parse_args(argv)

    settings = load_settings()
    if not settings.llm_configured:
        logger.warning("No LLM API key configured - policy_substance_quality and prohibited_content_risk will score as cannot_verify, not pass/fail.")

    stores = load_validation_set(Path(args.validation_set))
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    tallies: dict[str, CategoryTally] = {c: CategoryTally() for c in CATEGORIES}
    unknown_ground_truth: list[str] = []

    for store in stores:
        url = store["url"]
        logger.info("Auditing %s ...", url)
        try:
            findings = await run_one_store(url, settings)
        except SSRFBlockedError as exc:
            print(f"Skipping {url}: refused by SSRF guard - {exc}")
            continue
        except asyncio.TimeoutError:
            print(f"Skipping {url}: audit exceeded the {settings.audit_timeout_seconds}s time budget.")
            continue

        raw_path = results_dir / f"{url.replace('://', '_').replace('/', '_')}.json"
        raw_path.write_text(
            json.dumps([f.model_dump(mode="json") for f in findings], indent=2), encoding="utf-8",
        )

        for category in CATEGORIES:
            gt_entry = store["ground_truth"].get(category, {"verdict": "unknown"})
            gt_verdict = gt_entry.get("verdict", "unknown")
            if gt_verdict not in VALID_VERDICTS:
                print(f"Error: {url} category {category!r} has invalid verdict {gt_verdict!r} (expected one of {sorted(VALID_VERDICTS)}).")
                return 1

            tally = tallies[category]
            tool_says = tool_verdict_for_category(findings, category)

            if gt_verdict in ("not_applicable", "unknown"):
                tally.excluded += 1
                if gt_verdict == "unknown":
                    unknown_ground_truth.append(f"{url} / {category}")
                continue
            if tool_says == "cannot_verify":
                tally.excluded += 1
                continue

            tool_flagged = tool_says == "fail"
            truth_flagged = gt_verdict == "fail"
            if tool_flagged and truth_flagged:
                tally.tp += 1
            elif tool_flagged and not truth_flagged:
                tally.fp += 1
                tally.disagreements.append(f"{url}: tool flagged {category}, ground truth says pass")
            elif not tool_flagged and truth_flagged:
                tally.fn += 1
                tally.disagreements.append(f"{url}: tool missed {category} (ground truth: fail - {gt_entry.get('notes', '')})")
            else:
                tally.tn += 1

    print("\n=== Validation results (per category, not blended) ===\n")
    header = f"{'category':<38} {'TP':>3} {'FP':>3} {'TN':>3} {'FN':>3} {'excl':>4}  {'precision':>9}  {'recall':>7}"
    print(header)
    print("-" * len(header))
    for category in CATEGORIES:
        t = tallies[category]
        precision = f"{t.precision:.2f}" if t.precision is not None else "n/a"
        recall = f"{t.recall:.2f}" if t.recall is not None else "n/a"
        print(f"{category:<38} {t.tp:>3} {t.fp:>3} {t.tn:>3} {t.fn:>3} {t.excluded:>4}  {precision:>9}  {recall:>7}")

    any_disagreements = [d for t in tallies.values() for d in t.disagreements]
    if any_disagreements:
        print("\n=== Disagreements (false positives / false negatives) ===")
        for line in any_disagreements:
            print(f"- {line}")

    if unknown_ground_truth:
        print(f"\nWarning: {len(unknown_ground_truth)} category(ies) still have 'unknown' ground truth and were excluded from scoring:")
        for line in unknown_ground_truth:
            print(f"- {line}")

    print(f"\nRaw per-store findings written to {results_dir}/ for later inspection.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
