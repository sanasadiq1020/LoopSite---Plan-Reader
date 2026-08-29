"""Anonymous per-browser sessions — no login required, but every piece of
uploaded plan data is only ever readable by the session that created it.

The session is verified on every data-access request, not merely used to filter
what a list shows, which is what makes this real access control rather than
hiding rows in an interface.

**Why the session is not a cookie alone.** Deployed, the interface and the API
sit on different domains, so a cookie set by the API is a *third-party* cookie
to the page the reader is looking at — and browsers now block those by default.
The failure is silent and total: the cookie is dropped, every request arrives
without a session, and the reader is told their plan could not be processed.
Worse, it is invisible to whoever deployed it, because their own browser has
usually visited the API's domain directly at some point and so keeps the cookie.

So the browser holds its session itself and presents it explicitly:

*   an ``X-Session-Id`` header on anything fetched by script, and
*   an ``s`` query parameter on anything a browser loads by URL — an ``<img>``
    or a download link cannot carry a header.

The cookie is still set and still accepted. It costs nothing, and it is what
makes the local development setup work with no configuration at all.

None of this is a secret in the cryptographic sense: it is an anonymous
identifier that separates one reader's upload from another's on a tool that
requires no account. It is not a credential, and it protects nothing beyond the
plan a reader uploaded minutes ago.
"""

import re
import uuid

from fastapi import Request, Response

from app.settings import cookie_is_secure, cookie_samesite

SESSION_COOKIE_NAME = "loopsite_session"
SESSION_HEADER_NAME = "X-Session-Id"
SESSION_QUERY_NAME = "s"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days

# A session identifier is 32 hexadecimal characters and nothing else. Anything
# else presented as one is ignored rather than trusted: these values reach the
# filesystem through run ownership checks.
_VALID_SESSION = re.compile(r"^[0-9a-f]{32}$")


def _clean(value) -> str:
    value = (value or "").strip()
    return value if _VALID_SESSION.match(value) else ""


def new_session_id() -> str:
    return uuid.uuid4().hex


def get_session_id(request: Request, response: Response) -> str:
    """The session this request belongs to, creating one if it has none.

    Checked in order of how deliberate each is: a header is sent by the
    interface on purpose, a query parameter is attached to a URL on purpose,
    and a cookie is whatever the browser happened to keep.
    """
    session_id = (
        _clean(request.headers.get(SESSION_HEADER_NAME))
        or _clean(request.query_params.get(SESSION_QUERY_NAME))
        or _clean(request.cookies.get(SESSION_COOKIE_NAME))
    )
    if not session_id:
        session_id = new_session_id()

    # Always offered back as a cookie. Where it is accepted — same origin, or
    # local development — nothing else has to be arranged.
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite=cookie_samesite(),
        secure=cookie_is_secure(),
    )
    # So the interface can read it even where the cookie is refused.
    response.headers[SESSION_HEADER_NAME] = session_id
    return session_id
