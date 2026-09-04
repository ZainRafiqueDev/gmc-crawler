"""Section 4.3: resource consumption per request must be hard-capped
regardless of what a caller (CLI flag, future API request body) asks for.
"""
from app.config import (
    HARD_AUDIT_TIMEOUT_SECONDS,
    HARD_MAX_CONCURRENCY,
    HARD_MAX_DEPTH,
    HARD_MAX_IMAGE_BYTES,
    HARD_MAX_PAGES,
    Settings,
)


def test_crawl_max_pages_clamped_at_construction():
    assert Settings(crawl_max_pages=10_000_000).crawl_max_pages == HARD_MAX_PAGES


def test_crawl_max_depth_clamped_at_construction():
    assert Settings(crawl_max_depth=999).crawl_max_depth == HARD_MAX_DEPTH


def test_crawl_concurrency_clamped_at_construction():
    assert Settings(crawl_concurrency=999).crawl_concurrency == HARD_MAX_CONCURRENCY


def test_audit_timeout_clamped_at_construction():
    assert Settings(audit_timeout_seconds=999_999).audit_timeout_seconds == HARD_AUDIT_TIMEOUT_SECONDS


def test_max_image_bytes_clamped_at_construction():
    assert Settings(max_image_bytes=999_999_999).max_image_bytes == HARD_MAX_IMAGE_BYTES


def test_zero_or_negative_pages_clamped_up_to_at_least_one():
    assert Settings(crawl_max_pages=0).crawl_max_pages == 1
    assert Settings(crawl_max_pages=-5).crawl_max_pages == 1


def test_direct_attribute_assignment_after_construction_is_also_clamped():
    # audit.py sets settings.crawl_max_pages = args.max_pages directly after
    # construction (CLI override) - validate_assignment=True must catch this too,
    # not just the initial constructor call.
    settings = Settings()
    settings.crawl_max_pages = 10_000_000
    assert settings.crawl_max_pages == HARD_MAX_PAGES

    settings.crawl_max_depth = 999
    assert settings.crawl_max_depth == HARD_MAX_DEPTH


def test_reasonable_values_pass_through_unchanged():
    settings = Settings(crawl_max_pages=50, crawl_max_depth=3, crawl_concurrency=5)
    assert settings.crawl_max_pages == 50
    assert settings.crawl_max_depth == 3
    assert settings.crawl_concurrency == 5


def test_domain_min_delay_clamped_to_a_sane_range():
    assert Settings(crawl_domain_min_delay_seconds=-5).crawl_domain_min_delay_seconds == 0.0
    assert Settings(crawl_domain_min_delay_seconds=999).crawl_domain_min_delay_seconds == 30.0
    assert Settings(crawl_domain_min_delay_seconds=1.5).crawl_domain_min_delay_seconds == 1.5


def test_challenge_wait_seconds_clamped_to_a_sane_range():
    # Found live: a real store's bot-protection interstitial took longer to
    # resolve than PageFetcher's old hardcoded 6s default, producing a false
    # bot_blocked failure against a site that was actually reachable.
    assert Settings(crawl_challenge_wait_seconds=0).crawl_challenge_wait_seconds == 1.0
    assert Settings(crawl_challenge_wait_seconds=999).crawl_challenge_wait_seconds == 30.0
    assert Settings(crawl_challenge_wait_seconds=12.0).crawl_challenge_wait_seconds == 12.0
    assert Settings().crawl_challenge_wait_seconds == 10.0


# --- crawl_extra_headers (purchase-journey validation follow-up) ----------

def test_crawl_extra_headers_empty_string_parses_to_empty_dict():
    """Regression: an empty string is the documented .env.example "off"
    value for this field - it must never crash settings loading. Found live
    setting up purchase-journey validation: pydantic-settings auto-JSON-
    decodes a dict-typed env var *before* any field validator runs, and
    raises outright on "" rather than treating it as empty - confirmed by
    actually loading Settings via the environment, not just constructing it
    directly (that path bypasses the auto-decoding entirely and would have
    missed this)."""
    import os
    from app.config import load_settings

    os.environ["CRAWL_EXTRA_HEADERS"] = ""
    try:
        settings = load_settings()
    finally:
        del os.environ["CRAWL_EXTRA_HEADERS"]
    assert settings.crawl_extra_headers_dict == {}


def test_crawl_extra_headers_unset_parses_to_empty_dict():
    assert Settings().crawl_extra_headers_dict == {}


def test_crawl_extra_headers_valid_json_parses_to_dict():
    settings = Settings(crawl_extra_headers='{"ngrok-skip-browser-warning": "true"}')
    assert settings.crawl_extra_headers_dict == {"ngrok-skip-browser-warning": "true"}


def test_crawl_extra_headers_malformed_json_degrades_to_empty_dict_not_a_crash():
    settings = Settings(crawl_extra_headers="not valid json")
    assert settings.crawl_extra_headers_dict == {}
