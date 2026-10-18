"""Google connectors: OAuth web flow for Calendar (read-only) and Docs (export).
Plain httpx against Google's REST endpoints — no extra SDK.

Tokens persist in the local SQLite db so restarts / --reload keep you connected.
They are stored unencrypted: fine for local dev, TODO(P2) Secret Manager/Firestore for prod."""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import time
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import httpx

from ..database import fire, schema
from ..helper import net
from ..helper.config import settings

DB = Path(__file__).parent / "recal.db"
# Endpoints live in helper/config.py (env-overridable); aliased here for call sites/tests.
AUTH_URL = settings.GOOGLE_AUTH_URL
TOKEN_URL = settings.GOOGLE_TOKEN_URL
REVOKE_URL = settings.GOOGLE_REVOKE_URL
CAL_LIST_URL = settings.GOOGLE_CAL_LIST_URL
EVENTS_URL = settings.GOOGLE_EVENTS_URL   # {} = quoted calendar id
DOCS_URL = settings.GOOGLE_DOCS_URL
DOC_EDIT_URL = settings.GOOGLE_DOC_EDIT_URL   # {} = document id
CALLBACK = "/api/connect/google/callback"
SCOPES = {"calendar": settings.GOOGLE_SCOPE_CALENDAR,
          "gdocs": settings.GOOGLE_SCOPE_GDOCS}   # only files Recal creates

_pending: dict[str, dict] = {}     # oauth state -> {user, provider, return_to, at}


class GoogleError(Exception):
    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason, self.status = reason, status


# ---- token store (Firestore sealed envelope / local SQLite) ------------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS oauth_tokens(
        user_id TEXT, provider TEXT, data TEXT, PRIMARY KEY(user_id, provider))""")
    return conn


def _load(user: str, provider: str) -> dict | None:
    if fire.enabled():
        doc = fire.get_doc(*schema.connections(user), f"google_{provider}")
        try:
            data = json.loads(fire.unseal(doc["token"])) if doc else None
        except Exception:  # noqa: BLE001 — undecryptable token = not connected
            return None
        if data and fire.needs_reseal(doc["token"]):     # upgrade older, weaker sealing
            _save(user, provider, data)
        return data
    with _db() as c:
        row = c.execute("SELECT data FROM oauth_tokens WHERE user_id=? AND provider=?",
                        (user, provider)).fetchone()
    return json.loads(row[0]) if row else None


def _save(user: str, provider: str, data: dict):
    if fire.enabled():
        fire.set_doc({"token": fire.seal(json.dumps(data)), "email": data.get("email", ""),
                      "meta": fire.meta()}, *schema.connections(user), f"google_{provider}")
        return
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO oauth_tokens VALUES(?,?,?)",
                  (user, provider, json.dumps(data)))


def _delete(user: str, provider: str):
    if fire.enabled():
        fire.delete_doc(*schema.connections(user), f"google_{provider}")
        return
    with _db() as c:
        c.execute("DELETE FROM oauth_tokens WHERE user_id=? AND provider=?", (user, provider))


# ---- status ----------------------------------------------------------------
def configured() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def redirect_uri() -> str:
    return settings.OAUTH_REDIRECT_BASE.rstrip("/") + CALLBACK


def connected(user: str, provider: str) -> bool:
    return _load(user, provider) is not None


def status(user: str) -> dict:
    out = {"configured": configured(), "redirect_uri": redirect_uri()}
    for p in SCOPES:
        t = _load(user, p)
        out[p] = {"connected": bool(t), "email": t and t.get("email")}
    return out


# ---- OAuth flow --------------------------------------------------------------
def _safe_return(return_to: str) -> str:
    """Only bounce back to localhost or our own base URL (no open redirect)."""
    u = urlparse(return_to or "")
    if u.scheme in ("http", "https") and (u.hostname in ("localhost", "127.0.0.1")
                                          or return_to.startswith(settings.OAUTH_REDIRECT_BASE)):
        return f"{u.scheme}://{u.netloc}"
    return settings.OAUTH_REDIRECT_BASE.rstrip("/")


def auth_url(user: str, provider: str, return_to: str) -> str:
    if not configured():
        raise GoogleError("oauth_not_configured")
    now = time.time()
    for s, p in list(_pending.items()):
        if now - p["at"] > 600:
            _pending.pop(s, None)
    state = secrets.token_urlsafe(24)
    _pending[state] = {"user": user, "provider": provider,
                       "return_to": _safe_return(return_to), "at": now}
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": f"openid email {SCOPES[provider]}",
        "access_type": "offline", "prompt": "consent", "state": state})


def take_pending(state: str) -> dict | None:
    p = _pending.pop(state or "", None)
    return p if p and time.time() - p["at"] <= 600 else None


def _token_request(data: dict) -> dict:
    try:
        r = httpx.post(TOKEN_URL, timeout=15, verify=net.ssl_context(), data={
            **data, "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET})
    except httpx.HTTPError:
        raise GoogleError("google_unreachable", 502)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:   # invalid_grant · invalid_client · redirect_uri_mismatch
        raise GoogleError(body.get("error", f"token_http_{r.status_code}"), 400)
    return body


def _email(id_token: str | None) -> str:
    """Read the email claim; the token came straight from Google's token endpoint over TLS."""
    try:
        seg = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("email", "")
    except Exception:  # noqa: BLE001
        return ""


def finish(pending: dict, code: str):
    tok = _token_request({"code": code, "grant_type": "authorization_code",
                          "redirect_uri": redirect_uri()})
    if SCOPES[pending["provider"]] not in tok.get("scope", "").split():
        raise GoogleError("scope_not_granted")     # user unticked the permission checkbox
    _save(pending["user"], pending["provider"], {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600) - 60,
        "email": _email(tok.get("id_token"))})


def disconnect(user: str, provider: str):
    t = _load(user, provider)
    _delete(user, provider)
    if t:
        try:   # best effort: also revoke Recal's access on Google's side
            httpx.post(REVOKE_URL, data={"token": t.get("refresh_token") or t["access_token"]},
                       timeout=10, verify=net.ssl_context())
        except httpx.HTTPError:
            pass


# ---- authorized calls --------------------------------------------------------
def _access_token(user: str, provider: str) -> str:
    t = _load(user, provider)
    if not t:
        raise GoogleError("not_connected", 409)
    if time.time() < t["expires_at"]:
        return t["access_token"]
    if not t.get("refresh_token"):
        _delete(user, provider)
        raise GoogleError("reconnect_needed", 401)
    try:
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    except GoogleError as e:
        if e.reason == "invalid_grant":    # revoked, or 7-day expiry of a Testing-mode app
            _delete(user, provider)
            raise GoogleError("reconnect_needed", 401)
        raise
    t.update(access_token=tok["access_token"], expires_at=time.time() + tok.get("expires_in", 3600) - 60)
    _save(user, provider, t)
    return t["access_token"]


def _api(user: str, provider: str, method: str, url: str, **kw) -> dict:
    token = _access_token(user, provider)
    try:
        r = httpx.request(method, url, headers={"Authorization": f"Bearer {token}"},
                          timeout=20, verify=net.ssl_context(), **kw)
    except httpx.HTTPError:
        raise GoogleError("google_unreachable", 502)
    if r.status_code == 401:
        raise GoogleError("reconnect_needed", 401)
    if r.status_code >= 400:   # 403 here is usually "API not enabled in your Cloud project"
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text
        raise GoogleError(f"google_http_{r.status_code}: {msg[:300]}", 502)
    return r.json()


def _local(iso: str) -> datetime:
    """Google RFC3339 (with offset) -> naive local time, matching the events table."""
    return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)


def _calendars(user: str) -> list[dict]:
    """The calendars shown in the user's Google Calendar (ticked in the sidebar)."""
    items = _api(user, "calendar", "GET", CAL_LIST_URL, params={"maxResults": 250}).get("items", [])
    return [c for c in items if c.get("primary") or c.get("selected")]


def calendar_events(user: str, start: datetime, end: datetime) -> list[dict]:
    """Events across the user's visible calendars. `busy` is True only for timed events
    on calendars they own that aren't marked Free — those are what block study time;
    all-day, Free, holiday and other people's calendars are display-only."""
    out = []
    for cal in _calendars(user):
        own = bool(cal.get("primary")) or cal.get("accessRole") == "owner"
        source = "" if cal.get("primary") else cal.get("summary", "")
        tag = f"{zlib.crc32(cal['id'].encode()):08x}"     # event ids are only unique per calendar
        page = None
        while True:
            params = {"timeMin": start.astimezone().isoformat(), "timeMax": end.astimezone().isoformat(),
                      "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
            if page:
                params["pageToken"] = page
            data = _api(user, "calendar", "GET", EVENTS_URL.format(quote(cal["id"], safe="")),
                        params=params)
            for e in data.get("items", []):
                if any(a.get("self") and a.get("responseStatus") == "declined"
                       for a in e.get("attendees", [])):
                    continue
                s, en = e.get("start", {}), e.get("end", {})
                all_day = "date" in s
                if all_day:   # end date is exclusive, e.g. 10 Oct -> 11 Oct for one day
                    st, et = datetime.fromisoformat(s["date"]), datetime.fromisoformat(en.get("date", s["date"]))
                elif s.get("dateTime") and en.get("dateTime"):
                    st, et = _local(s["dateTime"]), _local(en["dateTime"])
                else:
                    continue
                out.append({"google_id": f"{e['id']}@{tag}", "title": e.get("summary") or "(busy)",
                            "start": st, "end": et, "all_day": all_day, "source": source,
                            "busy": own and not all_day and e.get("transparency") != "transparent"})
            page = data.get("nextPageToken")
            if not page:
                break
    return out


# ---- Docs export -------------------------------------------------------------
def _flatten(v) -> list[str]:
    if isinstance(v, dict):
        return [x for item in v.values() for x in _flatten(item)]
    if isinstance(v, list):
        return [x for item in v for x in _flatten(item)]
    return [str(v)] if v not in (None, "") else []


def _doc_blocks(card) -> list[tuple[str, str]]:
    """Card -> (paragraph style, text) lines."""
    H1, H2, N = "HEADING_1", "HEADING_2", "NORMAL_TEXT"
    p, b = card.payload, [(H1, card.title)]
    if card.type == "page":
        for line in p.get("markdown", "").splitlines():
            if line.startswith("# ") and line[2:].strip() == card.title.strip():
                continue
            if line.startswith("#"):
                b.append((H1 if line.startswith("# ") else H2, line.lstrip("#").strip()))
            elif line.strip():
                b.append((N, line))
    elif card.type == "reel":
        for i, s in enumerate(p.get("scene_script", {}).get("scenes", []), 1):
            b.append((H2, f"Scene {i} · {s.get('archetype', '')}"))
            b.append((N, s.get("narration", "")))
            if vals := _flatten(s.get("slots", {})):
                b.append((N, " · ".join(vals)))
    elif card.type == "plan":
        b.append((N, p.get("summary", "")))
        for s in p.get("sessions", []):
            b.append((N, f"• {str(s['start'])[:16].replace('T', ' ')} · {s['minutes']} min · {s['title']}"
                         + (f" ({s['why']})" if s.get("why") else "")))
    elif card.type == "calendar_diff":
        b.append((N, p.get("narrative", "")))
        for m in p.get("moves", []):
            b.append((N, f"• {m.get('action')}: {m.get('reason') or m.get('event_id')}"))
    elif card.type == "doc_review":
        b.append((N, p.get("headline", "")))
        if p.get("key_message"):
            b.append((N, "Key message: " + p["key_message"]))
        for sev in ("high", "medium", "low"):
            issues = [i for i in p.get("issues", []) if i.get("severity") == sev]
            if issues:
                b.append((H2, {"high": "Fix before sending", "medium": "Worth fixing",
                               "low": "Polish"}[sev]))
                for i in issues:
                    b.append((N, f"• {i.get('where', '')}: {i.get('issue', '')}"))
                    b.append((N, f"   Fix: {i.get('fix', '')}"))
    elif card.type == "source":
        b += [(N, p.get("url", "")), (N, f"{p.get('chunks', 0)} chunks · {p.get('concepts', 0)} concepts")]
    return b + [(N, ""), (N, "Exported from Recal")]


def export_card(user: str, card) -> str:
    """Create a Google Doc from a card; returns its URL."""
    doc = _api(user, "gdocs", "POST", DOCS_URL, json={"title": card.title[:100] or "Recal export"})
    text, styles, idx = "", [], 1
    for style, line in _doc_blocks(card):
        line = line.rstrip("\n") + "\n"
        n = len(line.encode("utf-16-le")) // 2           # Docs indexes are UTF-16 code units
        if style != "NORMAL_TEXT":
            styles.append({"updateParagraphStyle": {
                "range": {"startIndex": idx, "endIndex": idx + n},
                "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType"}})
        text, idx = text + line, idx + n
    _api(user, "gdocs", "POST", f"{DOCS_URL}/{doc['documentId']}:batchUpdate",
         json={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}] + styles})
    return DOC_EDIT_URL.format(doc["documentId"])
