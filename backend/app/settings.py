"""Deployment settings, read from the environment.

Nothing here is a secret and nothing here changes what the reader sees. These
are the handful of facts that differ between a laptop and a deployed server —
which browser origin is allowed to call the API, and whether the session cookie
has to survive a hop between two different domains.

Every one has a working local default, so the project still runs with no
environment set at all.
"""

import os

from app.logging_setup import get_logger

logger = get_logger()


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def allowed_origins() -> list[str]:
    """Which browser origins may call this API.

    Set ``ALLOWED_ORIGINS`` to the deployed interface's address — several may
    be given, separated by commas, which is how a preview deployment and the
    live one are allowed at once.

    Deliberately never ``*``: the API sends a session cookie, and a wildcard
    origin with credentials is both refused by browsers and wrong.
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000"]
    origins = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    logger.info(f"CORS: allowing {len(origins)} origin(s): {', '.join(origins)}")
    return origins


def cookie_is_cross_site() -> bool:
    """Whether the interface and the API sit on different domains.

    When they do — the usual arrangement, with the interface on one host and
    the API on another — the session cookie needs ``SameSite=None``, and a
    browser only accepts that together with ``Secure``. Getting this wrong does
    not raise an error: the cookie is silently dropped, every request looks
    like a new visitor, and nobody can see the plan they just uploaded.
    """
    return _flag("COOKIE_CROSS_SITE", False)


def cookie_is_secure() -> bool:
    """Whether the session cookie is HTTPS-only.

    Always true when cross-site, because a browser will not accept
    ``SameSite=None`` without it.
    """
    return _flag("COOKIE_SECURE", False) or cookie_is_cross_site()


def cookie_samesite() -> str:
    return "none" if cookie_is_cross_site() else "lax"


def describe() -> dict:
    """What this instance is configured as — written to the log at startup, so
    a misconfigured deployment says so in its first three lines rather than
    through a bug report."""
    return {
        "allowed_origins": allowed_origins(),
        "cookie_samesite": cookie_samesite(),
        "cookie_secure": cookie_is_secure(),
    }
