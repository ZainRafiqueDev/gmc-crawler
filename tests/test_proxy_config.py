"""Part 5.2 (broader crawl-robustness round): opt-in BYO residential/
rotating-proxy support, brought into scope only after live testing this
round found concrete evidence of IP/fingerprint-level blocking on real
sites that no amount of retry/backoff/politeness could get past, and only
on the user's explicit decision (never built silently)."""
from app.config import Settings
from app.proxy_config import ProxyRotator, build_proxy_rotator, to_httpx_proxy_url, to_playwright_proxy


def test_no_proxy_configured_by_default():
    assert build_proxy_rotator(Settings()) is None


def test_single_gateway_proxy_with_separate_credentials():
    settings = Settings(proxy_server="http://gw.provider.com:8000", proxy_username="user1", proxy_password="pass1")
    rotator = build_proxy_rotator(settings)
    assert rotator is not None
    cfg = rotator.next()
    assert cfg.server == "http://gw.provider.com:8000"
    assert cfg.username == "user1"
    assert cfg.password == "pass1"
    # A single-entry pool returns the same config every time - correct,
    # since the provider rotates the exit IP server-side for this shape.
    assert rotator.next().server == cfg.server


def test_proxy_pool_round_robins_client_side():
    settings = Settings(proxy_pool="http://u1:p1@proxy1.example:8000,http://u2:p2@proxy2.example:8001")
    rotator = build_proxy_rotator(settings)
    assert rotator is not None
    first = rotator.next()
    second = rotator.next()
    third = rotator.next()
    assert first.server == "http://proxy1.example:8000"
    assert first.username == "u1" and first.password == "p1"
    assert second.server == "http://proxy2.example:8001"
    assert second.username == "u2" and second.password == "p2"
    assert third.server == first.server  # cycled back around


def test_proxy_pool_takes_precedence_over_single_server():
    settings = Settings(proxy_server="http://ignored.example:9999", proxy_pool="http://only.example:8000")
    rotator = build_proxy_rotator(settings)
    assert rotator.next().server == "http://only.example:8000"


def test_to_playwright_proxy_shape():
    settings = Settings(proxy_server="http://gw.example:8000", proxy_username="u", proxy_password="p")
    cfg = build_proxy_rotator(settings).next()
    assert to_playwright_proxy(cfg) == {"server": "http://gw.example:8000", "username": "u", "password": "p"}


def test_to_playwright_proxy_omits_missing_credentials():
    from app.proxy_config import ProxyConfig
    cfg = ProxyConfig(server="http://gw.example:8000")
    assert to_playwright_proxy(cfg) == {"server": "http://gw.example:8000"}


def test_to_httpx_proxy_url_embeds_credentials():
    settings = Settings(proxy_server="http://gw.example:8000", proxy_username="u@ser", proxy_password="p:ass")
    cfg = build_proxy_rotator(settings).next()
    url = to_httpx_proxy_url(cfg)
    assert url.startswith("http://")
    assert "gw.example:8000" in url
    assert "u%40ser" in url  # special characters percent-encoded


def test_rotator_requires_at_least_one_config():
    import pytest
    with pytest.raises(ValueError):
        ProxyRotator([])
