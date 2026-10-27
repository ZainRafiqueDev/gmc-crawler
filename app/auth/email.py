"""Outgoing email - OTP codes only, today (app.auth.otp's callers via
app/api/auth.py's password/email request-otp routes). Plain smtplib over
STARTTLS, not a transactional-email API/SDK - see Settings.smtp_host's own
comment in app/config.py for why.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import Settings

logger = logging.getLogger("gmc_audit.auth.email")

_SUBJECTS = {
    "password_change": "Your GMC Compliance Checker password change code",
    "email_change": "Your GMC Compliance Checker email change code",
}


class EmailNotConfiguredError(RuntimeError):
    pass


def send_otp_email(to_email: str, code: str, purpose: str, settings: Settings) -> None:
    if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password:
        raise EmailNotConfiguredError("SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD are not configured")

    message = EmailMessage()
    message["Subject"] = _SUBJECTS[purpose]
    message["From"] = settings.smtp_from_email or settings.smtp_username
    message["To"] = to_email
    message.set_content(
        f"Your one-time code is: {code}\n\n"
        f"It expires in {settings.otp_ttl_minutes} minutes. If you didn't request this, you can ignore this email."
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    logger.info("Sent %s OTP email to %s", purpose, to_email)
