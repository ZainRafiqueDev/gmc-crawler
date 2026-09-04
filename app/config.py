"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard ceilings applied regardless of what any caller (CLI flag, API request
# body, .env) asks for - section 4.3 of the hardening round: bound resource
# consumption per request so a frontend can't be made to run an unbounded
# crawl just because a user passed a huge number. Settings-level defaults
# above these can still be smaller/saner; these are the absolute max.
HARD_MAX_PAGES = 500
HARD_MAX_DEPTH = 8
HARD_MAX_CONCURRENCY = 20
HARD_AUDIT_TIMEOUT_SECONDS = 1800  # 30 minutes wall-clock for one full audit
HARD_MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15MB - vision/resolution checks download the full image
# Ceiling on how many product pages check_editorial_quality/check_prohibited_content
# will grade per audit, regardless of catalog size (fixed-size LLM catalog sampling
# follow-up, Part 1.3 - option C: scale the sample with catalog size but keep it
# bounded, same "explicit override, hard ceiling underneath" shape as CRAWL_MAX_PAGES).
HARD_MAX_LLM_PRODUCT_SAMPLE = 50
# Ceiling on how many product pages the crawler will explore per category
# (per-category page caps follow-up, Part 1.2) - keeps a many-category store
# from dumping its whole page budget into the first few categories
# discovered, while every category-listing page itself is always crawled
# regardless of this cap (see app/site_mapper.py's looks_like_catalog_priority_url
# gating - the cap only ever applies to product-looking URLs).
HARD_MAX_PRODUCT_PAGES_PER_CATEGORY = 200


class Settings(BaseSettings):
    # validate_assignment=True: callers set settings.crawl_max_pages = ... directly
    # after construction (CLI flag overrides) - without this, the clamping
    # validators below only run once at construction and a later direct
    # assignment could bypass the hard caps entirely.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)

    llm_provider: str = "claude"  # "claude" | "openai"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Phase C's policy RAG index always uses OpenAI embeddings regardless of
    # LLM_PROVIDER (Claude has no embeddings API) - needs openai_api_key set
    # even when llm_provider="claude" for grading.
    openai_embedding_model: str = "text-embedding-3-small"

    wc_consumer_key: str = ""
    wc_consumer_secret: str = ""

    crawl_max_pages: int = 150
    # True only when a caller explicitly set crawl_max_pages for this run (CLI
    # --max-pages, API request body's max_pages) - adaptive page-budget sizing
    # (app.site_mapper, follow-up round: adaptive budget/per-category caps)
    # only overwrites crawl_max_pages when this is False, so an explicit
    # override always wins over a guess from the sitemap size signal.
    crawl_max_pages_explicit: bool = False
    crawl_max_depth: int = 4
    crawl_concurrency: int = 4
    audit_timeout_seconds: int = HARD_AUDIT_TIMEOUT_SECONDS
    max_image_bytes: int = HARD_MAX_IMAGE_BYTES
    # Minimum delay (seconds) this crawler waits between requests to the
    # same domain - politeness/rate-limit avoidance so the crawl's own pace
    # doesn't trip a target site's own rate limiting (broader real-world
    # crawl robustness round, Part 1.2). 0.5s is a conservative default;
    # PageFetcher itself defaults to 0.0 (opt-in) so unit tests/direct
    # callers stay fast - this is the real value wired up by app.site_mapper.
    crawl_domain_min_delay_seconds: float = 0.5

    # How long PageFetcher waits for a Cloudflare/DDoS-Guard-style JS
    # interstitial to resolve before giving up on that attempt (see
    # PageFetcher._wait_for_challenge_to_resolve). PageFetcher's own class
    # default is 6.0s and was never wired to a setting until this was found
    # live: a real store's interstitial (vellano.site, "please wait up to 5
    # seconds") took closer to ~7-10s to clear in a real browser tab, longer
    # than PageFetcher's 6s allowance, so every attempt failed as bot_blocked
    # even though the site was live and reachable by a real browser session.
    # 10.0s gives real, slightly-slower challenges more room without adding
    # much latency to genuinely-blocked pages, which are already capped well
    # below max_attempts by the block-repeat-cap logic in PageFetcher.
    crawl_challenge_wait_seconds: float = 10.0

    report_output_dir: Path = Path("./reports")

    database_url: str = "sqlite+aiosqlite:///./gmc_monitor.db"
    default_policy_watch_interval_days: int = 7

    # Retention policy (job persistence follow-up) - both tables would grow
    # unboundedly otherwise. AuditRun is per-store history (used for delta
    # reports, so pruning must never drop the single most recent one);
    # AuditJobRecord is ad-hoc "Run Audit" jobs with no natural grouping, so
    # it's pruned by age instead of by count.
    audit_run_retention_count: int = 10
    audit_job_retention_days: int = 30

    # Frontend API (app/api/main.py)
    api_cors_origin: str = "http://localhost:3000"
    audit_rate_limit_max_requests: int = 5
    audit_rate_limit_window_seconds: float = 3600.0

    # Opt-in BYO residential/rotating-proxy support (Part 5.2 of the broader
    # crawl-robustness round) - OFF by default (all empty = no proxy used
    # anywhere, zero behavior change). See app/proxy_config.py for the full
    # design rationale and the live evidence that motivated adding this.
    # This tool does not operate or bundle any proxy infrastructure itself -
    # these just let an operator point it at a proxy service they already have.
    proxy_server: str = ""    # single/rotating-gateway endpoint, e.g. "http://gw.provider.com:8000"
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_pool: str = ""      # comma-separated full proxy URLs for client-side round-robin; takes precedence over proxy_server if set

    # Opt-in, off by default ("" = no change to any request this tool
    # makes). Never a substitute for or interaction with the SSRF guard -
    # only ever adds a header to a request that guard already allowed.
    # Exists for the rare case where the *target itself* requires a
    # specific header to be reachable at all (a staging-environment gate, or
    # a tunnel provider's own anti-abuse interstitial - e.g. ngrok's
    # free-tier warning page, hit live setting up the purchase-journey
    # validation test store). Real GMC store audits never need this.
    #
    # Deliberately a plain string (JSON), not dict[str, str]: pydantic-
    # settings auto-JSON-decodes a dict-typed env var *before* any field
    # validator runs, and crashes outright on "" (the documented, intended
    # "off" value in .env.example) rather than treating it as empty -
    # confirmed live, not hypothetical. Parsed tolerantly below instead.
    crawl_extra_headers: str = ""

    @property
    def crawl_extra_headers_dict(self) -> dict[str, str]:
        if not self.crawl_extra_headers:
            return {}
        try:
            parsed = json.loads(self.crawl_extra_headers)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # Fixed-size LLM catalog sampling follow-up (Part 1.3, option C, user-confirmed
    # 2026-09-03): min(this cap, max(5, 5% of reachable product pages)), risk-weighted
    # rather than first-N-crawled. 15 is the confirmed default; raise per-deployment if
    # the added LLM spend (see app/llm/checks.py's _product_sample_size) is acceptable.
    llm_product_sample_cap: int = 15

    # Per-category page caps follow-up (Part 1.2, this round): once this many
    # product-looking pages have been enqueued for a given category, further
    # product URLs in that same category are skipped for the rest of this
    # crawl - category-listing pages themselves are never subject to this cap
    # (see app/site_mapper.py). 30 is a reasonable default for a compliance
    # audit (product pages within one category tend to look alike; a
    # representative sample matters more than exhaustive coverage of every
    # SKU), hard-capped at HARD_MAX_PRODUCT_PAGES_PER_CATEGORY.
    crawl_max_product_pages_per_category: int = 30

    @field_validator("crawl_max_pages")
    @classmethod
    def _clamp_max_pages(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_PAGES))

    @field_validator("crawl_max_depth")
    @classmethod
    def _clamp_max_depth(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_DEPTH))

    @field_validator("crawl_concurrency")
    @classmethod
    def _clamp_concurrency(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_CONCURRENCY))

    @field_validator("audit_timeout_seconds")
    @classmethod
    def _clamp_audit_timeout(cls, v: int) -> int:
        return max(30, min(v, HARD_AUDIT_TIMEOUT_SECONDS))

    @field_validator("max_image_bytes")
    @classmethod
    def _clamp_max_image_bytes(cls, v: int) -> int:
        return max(1024, min(v, HARD_MAX_IMAGE_BYTES))

    @field_validator("crawl_domain_min_delay_seconds")
    @classmethod
    def _clamp_domain_min_delay(cls, v: float) -> float:
        return max(0.0, min(v, 30.0))

    @field_validator("crawl_challenge_wait_seconds")
    @classmethod
    def _clamp_challenge_wait(cls, v: float) -> float:
        return max(1.0, min(v, 30.0))

    @field_validator("llm_product_sample_cap")
    @classmethod
    def _clamp_llm_product_sample_cap(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_LLM_PRODUCT_SAMPLE))

    @field_validator("crawl_max_product_pages_per_category")
    @classmethod
    def _clamp_max_product_pages_per_category(cls, v: int) -> int:
        return max(1, min(v, HARD_MAX_PRODUCT_PAGES_PER_CATEGORY))

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.anthropic_api_key)


def load_settings() -> Settings:
    return Settings()
