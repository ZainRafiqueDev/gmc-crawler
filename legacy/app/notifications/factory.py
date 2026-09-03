from __future__ import annotations

from app.config import Settings
from app.notifications.base import EmailNotifier
from app.notifications.email import LoggingEmailNotifier, ResendEmailNotifier


def get_email_notifier(settings: Settings) -> EmailNotifier:
    if settings.notifications_are_live:
        return ResendEmailNotifier(api_key=settings.resend_api_key or "", to_email=settings.alert_email_to or "")
    return LoggingEmailNotifier()
