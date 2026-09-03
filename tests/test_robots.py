import httpx
import pytest
import respx

from app.security.robots import RobotsChecker


@pytest.mark.asyncio
@respx.mock
async def test_disallowed_path_is_blocked():
    respx.get("https://shop.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /admin/\n")
    )
    checker = RobotsChecker("https://shop.example.com")
    await checker.load()
    assert checker.is_allowed("https://shop.example.com/admin/secret") is False
    assert checker.is_allowed("https://shop.example.com/products/widget") is True


@pytest.mark.asyncio
@respx.mock
async def test_missing_robots_txt_allows_everything():
    respx.get("https://shop.example.com/robots.txt").mock(return_value=httpx.Response(404))
    checker = RobotsChecker("https://shop.example.com")
    await checker.load()
    assert checker.is_allowed("https://shop.example.com/anything") is True


@pytest.mark.asyncio
@respx.mock
async def test_fetch_failure_fails_open():
    respx.get("https://shop.example.com/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    checker = RobotsChecker("https://shop.example.com")
    await checker.load()
    assert checker.is_allowed("https://shop.example.com/anything") is True


def test_is_allowed_before_load_fails_open():
    checker = RobotsChecker("https://shop.example.com")
    assert checker.is_allowed("https://shop.example.com/admin/") is True


@pytest.mark.asyncio
@respx.mock
async def test_disallow_all_blocks_everything():
    respx.get("https://shop.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    checker = RobotsChecker("https://shop.example.com")
    await checker.load()
    assert checker.is_allowed("https://shop.example.com/products/widget") is False
