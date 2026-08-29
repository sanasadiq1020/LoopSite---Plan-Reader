"""Anonymous per-browser sessions — no login required, but every piece of
uploaded plan data is only ever readable by the session that created it.

The token lives in an HttpOnly cookie (JavaScript can't read or forge it) and
is verified on every data-access request, not just used to filter a list —
see routers/plan.py. That is what makes this real access control rather than
just hiding items in the UI.

Production note (documented decision, revisit at deployment time — claude.md
Section 4): `secure=True` below should be enabled once the app is served over
HTTPS, and cross-domain deployments (frontend/backend on different domains)
will need `samesite="none"` + `secure=True` together for the cookie to survive
cross-site requests at all.
"""

import uuid

from app.settings import cookie_is_secure, cookie_samesite

from fastapi import Request, Response

SESSION_COOKIE_NAME = "loopsite_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days


def get_session_id(request: Request, response: Response) -> str:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        return session_id

    session_id = uuid.uuid4().hex
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        # Where the interface and the API sit on different domains — the usual
        # deployed arrangement — the cookie needs SameSite=None, and a browser
        # only accepts that with Secure. Getting it wrong raises no error: the
        # cookie is silently dropped and every request looks like a new
        # visitor, so nobody can see the plan they just uploaded.
        samesite=cookie_samesite(),
        secure=cookie_is_secure(),
    )
    return session_id
