"""The purchase-journey opt-in gate is safety-critical - test it directly
rather than relying only on manual CLI runs.
"""
import pytest

from audit import main_async


@pytest.mark.asyncio
async def test_enable_without_confirm_refuses_to_run(capsys):
    exit_code = await main_async(["--url", "https://example.com", "--enable-purchase-journey"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "requires --confirm-test-payment-mode" in captured.out


@pytest.mark.asyncio
async def test_confirm_without_enable_is_a_no_op_flag_not_an_error():
    # --confirm-test-payment-mode alone (without --enable-purchase-journey) should not
    # trip the gate - it's meaningless without --enable-purchase-journey, but harmless.
    from audit import parse_args
    args = parse_args(["--url", "https://example.com", "--confirm-test-payment-mode"])
    assert args.enable_purchase_journey is False
    assert args.confirm_test_payment_mode is True


def test_neither_flag_set_by_default():
    from audit import parse_args
    args = parse_args(["--url", "https://example.com"])
    assert args.enable_purchase_journey is False
    assert args.confirm_test_payment_mode is False


@pytest.mark.asyncio
async def test_ssrf_blocked_url_refuses_before_launching_a_browser(capsys):
    # 169.254.169.254 is a real, literal IP - assert_public_url resolves it
    # with no network access needed (getaddrinfo on a literal IP is instant),
    # so this exercises the real SSRF check, not a mock.
    exit_code = await main_async(["--url", "http://169.254.169.254/latest/meta-data/"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "refusing to audit" in captured.out.lower()
