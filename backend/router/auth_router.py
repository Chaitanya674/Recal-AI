"""Endpoints 4–6: auth (Google Sign-In / guest) and the connect flows
(Google Calendar, Google Docs -> NotebookLM)."""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..features import calendar as cal
from ..features import google
from ..helper.logger import get_logger
from ..schemas import AuthRequest, AuthResponse, UserOut
from .chat_router import current_user

router = APIRouter(prefix="/api")
log = get_logger(__name__)


@router.post("/auth")
def auth(body: AuthRequest, response: Response) -> AuthResponse:
    if body.guest or not body.id_token:
        response.set_cookie("recal_user", "demo", httponly=True, samesite="lax")
        return AuthResponse(ok=True, user=UserOut(id="demo", name="Guest", guest=True))
    # TODO(P5): verify id_token via google.oauth2.id_token.verify_oauth2_token
    uid = "u_" + uuid.uuid5(uuid.NAMESPACE_URL, body.id_token[:64]).hex[:10]
    response.set_cookie("recal_user", uid, httponly=True, samesite="lax")
    return AuthResponse(ok=True, user=UserOut(id=uid, name="Google user", guest=False))


# ---- Google: OAuth web flow for Calendar (read) + Docs (export) ---------------
_NOT_CONFIGURED = ("Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env "
                   "and restart the backend (TESTING.md, Google section).")


def _google_start(request: Request, provider: str, return_to: str):
    """Returns the consent URL; the frontend navigates the browser there."""
    try:
        url = google.auth_url(current_user(request), provider, return_to)
    except google.GoogleError as e:
        return JSONResponse({"ok": False, "reason": e.reason, "provider": provider,
                             "todo": _NOT_CONFIGURED}, status_code=e.status)
    return {"ok": True, "provider": provider, "auth_url": url}


@router.get("/connect/calendar")
def connect_calendar(request: Request, return_to: str = ""):
    return _google_start(request, "calendar", return_to)


@router.post("/connect/notebook")
def connect_notebook(body: dict, request: Request):
    return _google_start(request, "gdocs", body.get("return_to", ""))


@router.get("/connect/google/callback")
def google_callback(state: str = "", code: str = "", error: str = ""):
    """Google redirects here after consent; we bounce back to the UI with a result flag."""
    p = google.take_pending(state)
    if not p:
        return HTMLResponse("<p>This sign-in link expired or was already used. Close this tab "
                            "and click the pill in Recal again.</p>", status_code=400)
    back = p["return_to"] + "/?"
    if error:   # e.g. access_denied when the user clicks Cancel
        return RedirectResponse(back + urlencode({"connect_error": error, "provider": p["provider"]}))
    try:
        google.finish(p, code)
        if p["provider"] == "calendar":
            cal.sync_google(p["user"], force=True)
    except google.GoogleError as e:
        return RedirectResponse(back + urlencode({"connect_error": e.reason, "provider": p["provider"]}))
    except Exception:  # noqa: BLE001 — never strand the user on a bare 500 page
        log.exception("google callback failed for %s", p["provider"])
        return RedirectResponse(back + urlencode({"connect_error": "server_error",
                                                  "provider": p["provider"]}))
    return RedirectResponse(back + urlencode({"connected": p["provider"]}))


@router.get("/connect/google")
def google_status(request: Request):
    user = current_user(request)
    sync_error = None
    try:
        cal.sync_google(user)
    except google.GoogleError as e:
        sync_error = e.reason
    s = google.status(user)
    s["calendar"].update(synced_events=cal.google_event_count(user), sync_error=sync_error)
    return {"ok": True, **s}


@router.delete("/connect/calendar")
def disconnect_calendar(request: Request):
    user = current_user(request)
    google.disconnect(user, "calendar")
    cal.drop_google(user)
    return {"ok": True, "connected": False, "week": cal.week(user)}


@router.delete("/connect/gdocs")
def disconnect_gdocs(request: Request):
    google.disconnect(current_user(request), "gdocs")
    return {"ok": True, "connected": False}
