"""app.auth.email - just the "not configured" guard, which is the one
branch that never touches a real socket. Actually sending mail over SMTP
is exercised indirectly in tests/test_api_auth_password_email_change.py
(via a fake outbox, not a real server) - there's no fake SMTP server in
this project's test setup to assert the real smtplib path end to end.
"""
from __future__ import annotations

import pytest

from app.auth.email import EmailNotConfiguredError, send_otp_email
from app.config import Settings


def test_blank_smtp_host_raises_not_configured():
    settings = Settings(smtp_host="", smtp_username="", smtp_password="")
    with pytest.raises(EmailNotConfiguredError):
        send_otp_email("admin@example.com", "123456", "password_change", settings)


def test_smtp_configured_but_blank_username_raises_not_configured():
    settings = Settings(smtp_host="smtp.example.com", smtp_username="", smtp_password="hunter2")
    with pytest.raises(EmailNotConfiguredError):
        send_otp_email("admin@example.com", "123456", "password_change", settings)
