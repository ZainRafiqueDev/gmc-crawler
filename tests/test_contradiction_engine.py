"""Tests for app.contradiction_engine (work-order step 4, piece 3). Each
test targets one of the explicitly required cases: the cross-page return-
window conflict, the absent-is-not-conflict case, the below-threshold ->
needs_review case, and the SKU-keyed product comparison.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.contradiction_engine import evaluate_cross_page_contradiction, evaluate_per_resource_method_comparison
from app.db import Database, Evaluation, FirstAuditRun, Resource, ResourceFact


async def _new_run(db: Database) -> int:
    async with db.session() as session:
        run = FirstAuditRun(url="https://example.com", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def _add_resource(db: Database, run_id: int, url: str, resource_type: str) -> int:
    async with db.session() as session:
        resource = Resource(audit_run_id=run_id, url=url, resource_type=resource_type)
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
        return resource.id


async def _add_fact(
    db: Database, resource_id: int, fact: str, value: str, method: str, confidence: float,
    source_url: str = "https://example.com", source_text: str = "evidence", variant_key: str | None = None,
) -> None:
    async with db.session() as session:
        session.add(ResourceFact(
            resource_id=resource_id, fact=fact, value=value, source_url=source_url,
            source_text=source_text, method=method, confidence=confidence, variant_key=variant_key,
        ))
        await session.commit()


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/contradiction_test.db")
    await database.init()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_cross_page_return_window_conflict_fails_above_confidence_threshold(db):
    run_id = await _new_run(db)
    returns_page = await _add_resource(db, run_id, "https://example.com/returns", "returns_policy")
    faq_page = await _add_resource(db, run_id, "https://example.com/faq", "faq")

    await _add_fact(db, returns_page, "return.window_days", "30", "return_window_regex", 0.80, source_text="within 30 days")
    await _add_fact(db, faq_page, "return.window_days", "14", "return_window_regex", 0.80, source_text="14-day returns")

    async with db.session() as session:
        evaluation = await evaluate_cross_page_contradiction(
            session, run_id, rule_id="return_window_conflict", fact_key="return.window_days",
            source_types=["returns_policy", "faq", "product", "checkout"], min_confidence=0.75,
        )
        await session.commit()

    assert evaluation.result == "fail"
    assert evaluation.resource_id is None
    evidence = json.loads(evaluation.evidence_json)
    values = {o["value"] for o in evidence["observations"]}
    assert values == {"30", "14"}  # merchant sees both sides, not just "conflict"


@pytest.mark.asyncio
async def test_absent_on_one_page_is_not_a_conflict(db):
    run_id = await _new_run(db)
    returns_page = await _add_resource(db, run_id, "https://example.com/returns", "returns_policy")
    # FAQ page exists but never mentions a return window at all.
    await _add_resource(db, run_id, "https://example.com/faq", "faq")

    await _add_fact(db, returns_page, "return.window_days", "30", "return_window_regex", 0.80)

    async with db.session() as session:
        evaluation = await evaluate_cross_page_contradiction(
            session, run_id, rule_id="return_window_conflict", fact_key="return.window_days",
            source_types=["returns_policy", "faq"], min_confidence=0.75,
        )
    assert evaluation is None  # only one real value exists - nothing to compare

    async with db.session() as session:
        rows = (await session.execute(select(Evaluation))).scalars().all()
    assert rows == []  # no Evaluation row fabricated out of a single data point


@pytest.mark.asyncio
async def test_agreeing_values_produce_a_pass_evaluation(db):
    run_id = await _new_run(db)
    returns_page = await _add_resource(db, run_id, "https://example.com/returns", "returns_policy")
    faq_page = await _add_resource(db, run_id, "https://example.com/faq", "faq")

    await _add_fact(db, returns_page, "return.window_days", "30", "return_window_regex", 0.80)
    await _add_fact(db, faq_page, "return.window_days", "30.0", "return_window_regex", 0.80)  # same value, different formatting

    async with db.session() as session:
        evaluation = await evaluate_cross_page_contradiction(
            session, run_id, rule_id="return_window_conflict", fact_key="return.window_days",
            source_types=["returns_policy", "faq"], min_confidence=0.75,
        )
        await session.commit()

    assert evaluation.result == "pass"


@pytest.mark.asyncio
async def test_conflict_below_confidence_threshold_routes_to_needs_review(db):
    run_id = await _new_run(db)
    homepage = await _add_resource(db, run_id, "https://example.com", "homepage")
    contact_page = await _add_resource(db, run_id, "https://example.com/contact", "contact_about")

    # business.address_country via the weaker inferential method (0.60 base
    # confidence) - below a 0.75 threshold.
    await _add_fact(db, homepage, "business.address_country", "US", "calling_code_cross_reference", 0.60)
    await _add_fact(db, contact_page, "business.address_country", "GB", "calling_code_cross_reference", 0.60)

    async with db.session() as session:
        evaluation = await evaluate_cross_page_contradiction(
            session, run_id, rule_id="business_identity_conflict", fact_key="business.address_country",
            source_types=None, min_confidence=0.75,
        )
        await session.commit()

    assert evaluation.result == "needs_review"
    assert evaluation.confidence == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_sku_keyed_product_price_comparison_jsonld_vs_platform_api(db):
    run_id = await _new_run(db)
    product_page = await _add_resource(db, run_id, "https://example.com/product/widget", "product")

    # Two variants on the SAME product page - must not collapse into one comparison.
    await _add_fact(db, product_page, "product.price.amount", "19.99", "jsonld", 0.95, variant_key="RED")
    await _add_fact(db, product_page, "product.price.amount", "19.99", "platform_api", 0.98, variant_key="RED")
    await _add_fact(db, product_page, "product.price.amount", "24.99", "jsonld", 0.95, variant_key="BLUE")
    await _add_fact(db, product_page, "product.price.amount", "29.99", "platform_api", 0.98, variant_key="BLUE")

    async with db.session() as session:
        evaluations = await evaluate_per_resource_method_comparison(
            session, run_id, rule_id="price_mismatch_jsonld_vs_platform", fact_key="product.price.amount",
            methods=("jsonld", "platform_api"), tolerance=0.01, min_confidence=0.90,
        )
        await session.commit()

    assert len(evaluations) == 2
    by_result = {e.result for e in evaluations}
    assert by_result == {"pass", "fail"}

    fail_eval = next(e for e in evaluations if e.result == "fail")
    evidence = json.loads(fail_eval.evidence_json)
    values = {o["value"] for o in evidence["observations"]}
    assert values == {"24.99", "29.99"}  # the BLUE variant's mismatch, not RED's


@pytest.mark.asyncio
async def test_different_products_never_compared_to_each_other(db):
    run_id = await _new_run(db)
    product_a = await _add_resource(db, run_id, "https://example.com/product/a", "product")
    product_b = await _add_resource(db, run_id, "https://example.com/product/b", "product")

    await _add_fact(db, product_a, "product.price.amount", "10.00", "jsonld", 0.95)
    await _add_fact(db, product_b, "product.price.amount", "50.00", "platform_api", 0.98)

    async with db.session() as session:
        evaluations = await evaluate_per_resource_method_comparison(
            session, run_id, rule_id="price_mismatch_jsonld_vs_platform", fact_key="product.price.amount",
            methods=("jsonld", "platform_api"), tolerance=0.01, min_confidence=0.90,
        )
    assert evaluations == []  # each product only has ONE method's observation - nothing to compare, and never cross-matched


@pytest.mark.asyncio
async def test_price_within_tolerance_is_a_pass_not_a_fail(db):
    run_id = await _new_run(db)
    product_page = await _add_resource(db, run_id, "https://example.com/product/widget", "product")
    await _add_fact(db, product_page, "product.price.amount", "19.99", "jsonld", 0.95)
    await _add_fact(db, product_page, "product.price.amount", "19.995", "platform_api", 0.98)  # 0.005 off - within 0.01 tolerance

    async with db.session() as session:
        evaluations = await evaluate_per_resource_method_comparison(
            session, run_id, rule_id="price_mismatch_jsonld_vs_platform", fact_key="product.price.amount",
            methods=("jsonld", "platform_api"), tolerance=0.01, min_confidence=0.90,
        )
        await session.commit()

    assert len(evaluations) == 1
    assert evaluations[0].result == "pass"
