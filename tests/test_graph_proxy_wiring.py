"""Part 5.2: app.graph._current_proxy_for is the single point that decides
what proxy (if any) the whole audit's non-Playwright (httpx) traffic uses -
platform detection runs before the crawl node, so this must be set once for
the whole run, not only inside app.site_mapper.map_site's own scope."""
from app.config import Settings
from app.graph import _current_proxy_for


def test_no_proxy_configured_by_default():
    assert _current_proxy_for(Settings()) is None


def test_single_server_proxy_becomes_one_httpx_url():
    settings = Settings(proxy_server="http://gw.example:8000", proxy_username="u", proxy_password="p")
    assert _current_proxy_for(settings) == "http://u:p@gw.example:8000"


def test_proxy_pool_uses_first_entry_for_this_whole_run():
    """httpx traffic gets one proxy for the whole audit run (not per-request
    rotation) - the Playwright side (app.fetch.PageFetcher) is what actually
    rotates per fetch attempt, since that's the high-volume path most worth
    varying."""
    settings = Settings(proxy_pool="http://u1:p1@proxy1.example:8000,http://u2:p2@proxy2.example:8001")
    assert _current_proxy_for(settings) == "http://u1:p1@proxy1.example:8000"
