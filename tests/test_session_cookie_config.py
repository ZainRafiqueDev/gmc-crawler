"""Settings.session_cookie_secure/session_cookie_samesite - cross-site
deployment (Vercel frontend + a separate backend host) needs SameSite=None,
which browsers reject unless the cookie is also Secure. This must fail at
config-construction time, not ship a cookie that looks set but is silently
never stored.
"""
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_are_the_local_dev_friendly_values():
    settings = Settings()
    assert settings.session_cookie_secure is False
    assert settings.session_cookie_samesite == "lax"


def test_samesite_none_without_secure_is_rejected():
    with pytest.raises(ValidationError, match="session_cookie_samesite='none' requires session_cookie_secure=true"):
        Settings(session_cookie_samesite="none", session_cookie_secure=False)


def test_samesite_none_with_secure_is_accepted():
    settings = Settings(session_cookie_samesite="none", session_cookie_secure=True)
    assert settings.session_cookie_samesite == "none"
    assert settings.session_cookie_secure is True


def test_invalid_samesite_value_is_rejected():
    with pytest.raises(ValidationError, match='session_cookie_samesite must be "lax", "strict", or "none"'):
        Settings(session_cookie_samesite="whatever")


def test_samesite_value_is_lowercased():
    assert Settings(session_cookie_samesite="LAX").session_cookie_samesite == "lax"
