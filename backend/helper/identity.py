"""Who is calling: every browser gets its own private guest profile.

The first /api request from a browser without a valid id is assigned a random,
unguessable guest id (96 bits) in an HttpOnly cookie; everything the app stores —
chats, cards, sessions, knowledge library, uploads, Google connections — is keyed by
it, so two visitors never see each other's data. The old shared `demo` id (and any
other malformed value) is rejected and replaced, never reused.

main.py's middleware calls assign() once per request; routers read current_user()."""
from __future__ import annotations

import re
import secrets

from fastapi import Request, Response

COOKIE = "recal_user"
MAX_AGE_S = 180 * 24 * 3600
_GUEST = re.compile(r"g_[0-9a-f]{24}")


def new_guest_id() -> str:
    return "g_" + secrets.token_hex(12)


def valid(uid: str | None) -> bool:
    return bool(uid) and bool(_GUEST.fullmatch(uid))


def assign(request: Request) -> str | None:
    """Pin this request to a user. Returns the NEW id when one had to be issued
    (the caller sets the cookie on the response), else None."""
    uid = request.cookies.get(COOKIE)
    if valid(uid):
        request.state.user = uid
        return None
    request.state.user = new_guest_id()
    return request.state.user


def remember(request: Request, response: Response, uid: str):
    response.set_cookie(COOKIE, uid, max_age=MAX_AGE_S, path="/", httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")


def current_user(request: Request) -> str:
    uid = getattr(request.state, "user", None)
    if uid:
        return uid
    uid = request.cookies.get(COOKIE)       # outside the middleware (e.g. tests)
    return uid if valid(uid) else assign(request)
