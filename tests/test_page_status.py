"""app.report._page_status: a page's [STATUS] tag in the Page-by-Page
Findings section. Regression coverage for a real bug found live - an
unreachable page (confirmed 404, or a page that failed mid-crawl) with no
findings against it was showing as [PASS], which reads as "checked, fine"
rather than "couldn't load it"."""
from app.models import CrawledPage, PageType
from app.report import _page_status


def _page(reachable: bool, cannot_verify: bool = False) -> CrawledPage:
    return CrawledPage(
        url="https://shop.example/page", page_type=PageType.BLOG_OTHER, depth=1,
        reachable=reachable, cannot_verify=cannot_verify,
    )


def test_unreachable_page_with_no_findings_is_not_pass():
    status = _page_status(_page(reachable=False), [])
    assert status == "UNREACHABLE"
    assert status != "PASS"


def test_cannot_verify_page_still_reads_as_cannot_verify_not_unreachable():
    status = _page_status(_page(reachable=False, cannot_verify=True), [])
    assert status == "CANNOT VERIFY"


def test_reachable_page_with_no_findings_is_pass():
    assert _page_status(_page(reachable=True), []) == "PASS"
