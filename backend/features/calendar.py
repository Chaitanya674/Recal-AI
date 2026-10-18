"""Calendar + planning. Storage is dual-mode (docs/firestore-design.md):
Firestore `users/{uid}/events/{id}` when GOOGLE_CLOUD_PROJECT is set, local SQLite
otherwise. Recal's own study/urgent events plus the Google Calendar mirror live here.
No demo data: a new user starts with an empty week. The slot-ID validation gate lives here."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from ..database import fire, schema
from ..helper import prompt_loader
from ..helper.config import settings
from . import google
from .llm import route
from ..schemas import Slot

DB = Path(__file__).parent / "recal.db"
DAY_START, DAY_END = dtime(7, 0), dtime(22, 30)
# "find a slot in the evening" must mean evening — windows gate slot enumeration.
WINDOWS = {"morning": (dtime(7, 0), dtime(12, 0)),
           "afternoon": (dtime(12, 0), dtime(17, 0)),
           "evening": (dtime(17, 0), dtime(22, 30))}

# busy=0: shown on the week but doesn't block study time (Google "Free", all-day,
# holiday/shared calendars). source: Google calendar name for non-primary calendars.
_ADDED_COLUMNS = {"busy": "INTEGER DEFAULT 1", "all_day": "INTEGER DEFAULT 0",
                  "source": "TEXT DEFAULT ''"}
_migrated = False


def db() -> sqlite3.Connection:
    global _migrated
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        id TEXT PRIMARY KEY, user_id TEXT, title TEXT, start TEXT, end TEXT,
        kind TEXT, status TEXT DEFAULT 'planned', topic TEXT DEFAULT '',
        resources TEXT DEFAULT '[]', moved_from TEXT DEFAULT '', why TEXT DEFAULT '',
        busy INTEGER DEFAULT 1, all_day INTEGER DEFAULT 0, source TEXT DEFAULT '')""")
    if not _migrated:     # older recal.db files predate these columns
        have = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        for col, ddl in _ADDED_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
        _migrated = True
    return conn


def _nid() -> str:
    return uuid.uuid4().hex[:12]


def _monday() -> datetime:
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# storage primitives (Firestore / SQLite behind the same three calls)
# ---------------------------------------------------------------------------
_FIELDS = ("id", "title", "start", "end", "kind", "status", "topic", "resources",
           "moved_from", "why", "busy", "all_day", "source")


def _events(user: str) -> list[dict]:
    """Every event of this user, resources as a list, sorted by start."""
    if fire.enabled():
        rows = [dict(data, id=doc_id) for doc_id, data in fire.stream(*schema.events(user))]
    else:
        with db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM events WHERE user_id=?", (user,)).fetchall()]
    out = []
    for r in rows:
        r.pop("user_id", None)
        r.pop("meta", None)
        if isinstance(r.get("resources"), str):
            r["resources"] = json.loads(r["resources"] or "[]")
        r.setdefault("resources", [])
        out.append({k: r.get(k, "") for k in _FIELDS})
    return sorted(out, key=lambda e: e["start"])


def _put(user: str, row: dict):
    row = {**{"status": "planned", "topic": "", "resources": [], "moved_from": "",
              "why": "", "busy": 1, "all_day": 0, "source": ""}, **row}
    if fire.enabled():
        fire.set_doc({**{k: row[k] for k in _FIELDS if k != "id"}, "meta": fire.meta()},
                     *schema.events(user), row["id"])
    else:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO events(id,user_id,title,start,end,kind,status,"
                      "topic,resources,moved_from,why,busy,all_day,source) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (row["id"], user, row["title"], row["start"], row["end"], row["kind"],
                       row["status"], row["topic"], json.dumps(row["resources"]),
                       row["moved_from"], row["why"], int(row["busy"]), int(row["all_day"]),
                       row["source"]))


def _delete_where(user: str, keep_gcal: bool | None):
    """keep_gcal=True: delete only gcal rows. False: only non-gcal. None: everything."""
    if fire.enabled():
        for doc_id, _ in fire.stream(*schema.events(user)):
            if keep_gcal is None or doc_id.startswith("gcal:") == keep_gcal:
                fire.delete_doc(*schema.events(user), doc_id)
    else:
        cond = {None: "", True: " AND id LIKE 'gcal:%'", False: " AND id NOT LIKE 'gcal:%'"}[keep_gcal]
        with db() as c:
            c.execute(f"DELETE FROM events WHERE user_id=?{cond}", (user,))


def reset_week(user: str):
    """Clear everything Recal created (study sessions, urgent blocks) and re-sync Google."""
    _delete_where(user, keep_gcal=False)
    try:
        sync_google(user, force=True)
    except google.GoogleError:
        pass     # the next chat turn retries and reports the failure


# ---- Google Calendar mirror --------------------------------------------------
# Connected users' Google events (every calendar ticked in Google Calendar) are mirrored
# into events (id "gcal:<user>:<id>", kind work) so week(), free_slots() and replan()
# treat them like any meeting; busy=0 rows are display-only.
GCAL_TTL_S = 60
_gcal_synced: dict[str, float] = {}


def sync_google(user: str, force: bool = False) -> int | None:
    """Refresh the mirror; returns events synced, or None if skipped. Raises GoogleError."""
    if not google.connected(user, "calendar"):
        return None
    if not force and time.time() - _gcal_synced.get(user, 0) < GCAL_TTL_S:
        return None
    monday = _monday()
    events = google.calendar_events(user, monday, monday + timedelta(days=14))
    _delete_where(user, keep_gcal=True)
    for e in events:
        _put(user, {"id": f"gcal:{user}:{e['google_id']}", "title": e["title"],
                    "start": e["start"].isoformat(), "end": e["end"].isoformat(),
                    "kind": "work", "why": "google calendar", "busy": int(e["busy"]),
                    "all_day": int(e["all_day"]), "source": e["source"]})
    _gcal_synced[user] = time.time()
    return len(events)


def drop_google(user: str):
    """After disconnect: remove the mirrored Google events."""
    _gcal_synced.pop(user, None)
    _delete_where(user, keep_gcal=True)


def google_event_count(user: str) -> int:
    return sum(1 for e in _events(user) if e["id"].startswith("gcal:"))


# ---------------------------------------------------------------------------
def week(user: str) -> list[dict]:
    start, end = _monday(), _monday() + timedelta(days=10)
    return [e for e in _events(user)
            if _dt(e["start"]) < end and _dt(e["end"]) > start]


def free_slots(user: str, days: int = 10, min_minutes: int = 45,
               prefer: str = "", ignore: set[str] | None = None,
               start_day: int = 0) -> list[Slot]:
    """Free gaps, earliest first, on `days` days starting `start_day` days from today.
    `prefer` ("morning"/"afternoon"/"evening") narrows the enumeration to that window —
    the daily study budget still counts the whole day.
    `ignore`: event ids treated as already gone (sessions that are being moved)."""
    win_lo, win_hi = WINDOWS.get(prefer, (DAY_START, DAY_END))
    all_events = [e for e in _events(user) if e["id"] not in (ignore or ())]
    slots: list[Slot] = []
    for d in range(start_day, start_day + days):
        day = (datetime.now() + timedelta(days=d)).date()
        day_lo, day_hi = datetime.combine(day, DAY_START), datetime.combine(day, DAY_END)
        lo, hi = datetime.combine(day, win_lo), datetime.combine(day, win_hi)
        todays = [e for e in all_events if _dt(e["start"]) < day_hi and _dt(e["end"]) > day_lo]
        busy = sorted((_dt(e["start"]), _dt(e["end"])) for e in todays
                      if e.get("busy", 1) and _dt(e["start"]) < hi and _dt(e["end"]) > lo)
        studied = sum((_dt(e["end"]) - _dt(e["start"])).seconds // 60 for e in todays
                      if e["kind"] in ("study", "review") and e["status"] != "missed")
        budget = max(0, settings.MAX_STUDY_MIN_PER_DAY - studied)
        cursor = max(lo, datetime.now() + timedelta(minutes=30))
        for b_start, b_end in busy + [(hi, hi)]:
            gap = int((b_start - cursor).total_seconds() // 60)
            if gap >= min_minutes and budget > 0:
                usable = min(gap, budget)
                slots.append(Slot(slot_id=f"S{len(slots)}", start=cursor,
                                  max_minutes=usable, day=day.strftime("%a %d %b")))
                budget -= usable
            cursor = max(cursor, b_end)
    return slots


def add_event(user: str, title: str, start: datetime, minutes: int, kind: str,
              topic: str = "", resources: list | None = None, why: str = "") -> str:
    eid = _nid()
    _put(user, {"id": eid, "title": title, "start": start.isoformat(),
                "end": (start + timedelta(minutes=minutes)).isoformat(), "kind": kind,
                "topic": topic, "resources": resources or [], "why": why})
    return eid


def move_event(user: str, event_id: str, new_start: datetime,
               minutes: int | None, why: str):
    row = next((e for e in _events(user) if e["id"] == event_id), None)
    if not row:
        return
    mins = minutes or (_dt(row["end"]) - _dt(row["start"])).seconds // 60
    _put(user, {**row, "moved_from": row["start"], "start": new_start.isoformat(),
                "end": (new_start + timedelta(minutes=mins)).isoformat(),
                "why": why, "status": "planned"})


def _delete_one(user: str, event_id: str):
    if fire.enabled():
        fire.delete_doc(*schema.events(user), event_id)
    else:
        with db() as c:
            c.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, user))


def edit_session(user: str, event_id: str, action: str,
                 start: str | datetime | None = None,
                 minutes: int | None = None) -> dict:
    """Edit ONE existing Recal-created session (the Concierge's edit tool).
    action: move (needs start, ISO datetime) | shrink (needs minutes) | delete.
    Google-mirrored events are read-only. Moves are refused when they would
    overlap a busy event. Returns {"ok", "reason"?, "week"}."""
    row = next((e for e in _events(user) if e["id"] == event_id), None)
    if not row:
        return {"ok": False, "reason": "event_not_found", "week": week(user)}
    if row["id"].startswith("gcal:"):
        return {"ok": False, "reason": "google_event_read_only", "week": week(user)}
    if action == "delete":
        _delete_one(user, event_id)
    elif action == "shrink":
        if not minutes:
            return {"ok": False, "reason": "minutes_required", "week": week(user)}
        mins = max(15, int(minutes))
        _put(user, {**row, "end": (_dt(row["start"]) + timedelta(minutes=mins)).isoformat(),
                    "why": "shrunk by user"})
    elif action == "move":
        if not start:
            return {"ok": False, "reason": "start_required", "week": week(user)}
        new_start = start if isinstance(start, datetime) else _dt(str(start))
        mins = int(minutes) if minutes else (_dt(row["end"]) - _dt(row["start"])).seconds // 60
        new_end = new_start + timedelta(minutes=mins)
        for e in _events(user):
            if (e["id"] != event_id and e.get("busy", 1)
                    and _dt(e["start"]) < new_end and new_start < _dt(e["end"])):
                return {"ok": False, "reason": f"overlaps '{e['title']}'", "week": week(user)}
        move_event(user, event_id, new_start, mins, "moved by user")
    else:
        return {"ok": False, "reason": "bad_action", "week": week(user)}
    return {"ok": True, "week": week(user)}


def set_status(user: str, event_id: str, status: str):
    if fire.enabled():
        fire.set_doc({"status": status, "meta": fire.meta()}, *schema.events(user), event_id,
                     merge=True)
    else:
        with db() as c:
            c.execute("UPDATE events SET status=? WHERE id=? AND user_id=?",
                      (status, event_id, user))


def slots_valid(moves_out: dict, slots: list[Slot]) -> tuple[bool, str]:
    """THE verification gate: every move/shrink must target an enumerated slot_id."""
    ids = {s.slot_id for s in slots}
    for m in moves_out.get("moves", []):
        if m.get("action") in ("move", "shrink") and m.get("to_slot_id") not in ids:
            return False, f"unknown slot_id {m.get('to_slot_id')!r}"
    return True, ""


# ---------------------------------------------------------------------------
def plan(user: str, goal: str, hours: float | None = None,
         deadline_days: int = 7, prefer: str = "", on_date: str = "") -> tuple[dict, object]:
    """Decompose + place. `hours` is a HARD budget (the user's own words — never
    overshoot it); `on_date` ("YYYY-MM-DD") pins every session to that day, while
    `deadline_days` allows today through that many days ahead, inclusive. `prefer`
    narrows placement to morning/afternoon/evening slots, falling back to other
    times of the same window (with a note) only when that part of the day is full."""
    note = ""
    offset = (date.fromisoformat(on_date) - datetime.now().date()).days if on_date else -1
    if offset >= 0:
        start, span = offset, 1
        where = f"on {date.fromisoformat(on_date):%a %d %b}"
    else:
        start, span = 0, max(0, deadline_days) + 1
        where = "before the deadline"
    slots = free_slots(user, days=span, prefer=prefer, start_day=start)
    if prefer and not slots:
        slots = free_slots(user, days=span, start_day=start)
        note = f"no free {prefer} time {where} — placed at other times {where}"
    if not slots:
        note = f"no free time {where} — nothing booked"
    budget = int(hours * 60) if hours else None
    total = int(min(sum(s.max_minutes for s in slots), budget or 5 * 60))
    mock = {"summary": f"{goal}: fundamentals first, hands-on before review.",
            "subtopics": [
                {"title": f"{goal}: mental model", "minutes": 60, "why": "prerequisite for everything"},
                {"title": f"{goal}: hands-on build", "minutes": 75, "why": "retention via practice"},
                {"title": f"{goal}: edge cases + review", "minutes": 60, "why": "spaced recall anchor"},
            ]}
    out, receipt = route("plan", prompt_loader.get("plan_decompose", goal=goal, total=total),
                         mock_output=mock, domain=goal[:24])
    sessions, si, placed = [], iter(slots), 0
    cur = next(si, None)
    for sub in out.get("subtopics", []):
        if budget is not None and budget - placed < 30:
            break                                   # the user said N hours: never overshoot
        need = max(30, min(int(sub.get("minutes", 60)), 120))
        if budget is not None:
            need = min(need, budget - placed)
        while cur and cur.max_minutes < 30:
            cur = next(si, None)
        if not cur:
            break
        mins = min(need, cur.max_minutes)
        eid = add_event(user, f"Study: {sub['title']}", cur.start, mins,
                        "study", topic=goal, why=sub.get("why", ""))
        sessions.append({"event_id": eid, "title": sub["title"],
                         "start": cur.start.isoformat(), "minutes": mins,
                         "why": sub.get("why", ""), "resources": []})
        placed += mins
        rem = cur.max_minutes - mins
        cur = (Slot(slot_id=cur.slot_id, start=cur.start + timedelta(minutes=mins),
                    max_minutes=rem, day=cur.day) if rem >= 30 else next(si, None))
    summary = out.get("summary", "") + (f" ({note})" if note else "")
    return {"summary": summary, "sessions": sessions}, receipt


def replan(user: str, energy: str = "normal") -> tuple[dict, object]:
    now = datetime.now()
    slots = free_slots(user)
    events = week(user)
    overdue = [e for e in events if e["kind"] in ("study", "review")
               and e["status"] == "planned" and _dt(e["end"]) < now]
    upcoming = [e for e in events if e["kind"] in ("study", "review")
                and _dt(e["start"]) > now]
    mock_moves = {"narrative": "Shifted your next session out of the collision and kept "
                               "tomorrow light.",
                  "moves": ([{"event_id": upcoming[0]["id"], "action": "move",
                              "to_slot_id": slots[0].slot_id, "minutes": None,
                              "reason": "urgent work took this slot"}]
                            if upcoming and slots else [])}
    out, receipt = route(
        "replan",
        "Renegotiate. Only these slot_ids: "
        + json.dumps([s.model_dump(mode='json') for s in slots])
        + f". energy={energy}. overdue={json.dumps(overdue)} upcoming="
        + json.dumps([{ 'id': e['id'], 'title': e['title'], 'start': e['start']} for e in upcoming])
        + ' JSON {"moves":[{"event_id","action":"move|shrink|drop","to_slot_id","minutes","reason"}],"narrative"}',
        verifier=lambda o: slots_valid(o, slots), mock_output=mock_moves, domain="replan")
    slot_by_id = {s.slot_id: s for s in slots}
    applied = []
    for m in out.get("moves", []):
        if m["action"] == "drop":
            set_status(user, m["event_id"], "missed")
        else:
            s = slot_by_id.get(m.get("to_slot_id"))
            if s:
                move_event(user, m["event_id"], s.start, m.get("minutes"), m.get("reason", ""))
        applied.append(m)
    return {"narrative": out.get("narrative", ""), "moves": applied}, receipt


def _editable(user: str) -> list[dict]:
    """Recal's own study/review sessions the user can still change."""
    now = datetime.now()
    return [e for e in week(user) if not e["id"].startswith("gcal:")
            and e["kind"] in ("study", "review") and e["status"] != "done"
            and _dt(e["end"]) > now - timedelta(days=1)]


def _edit_check(out: dict, sessions: dict[str, dict], user: str) -> tuple[bool, str]:
    """Verification gate for model-proposed edits: known event ids only, and every
    move lands inside a real free window (with the moved sessions' own time freed)."""
    if not isinstance(out, dict) or not isinstance(out.get("edits"), list):
        return False, "missing edits list"
    if not out["edits"] and not str(out.get("question", "")).strip():
        return False, "no edits and no clarifying question"
    moved = {str(e.get("event_id")) for e in out["edits"]
             if isinstance(e, dict) and e.get("action") == "move"}
    windows = free_slots(user, min_minutes=15, ignore=moved) if moved else []
    for e in out["edits"]:
        if not isinstance(e, dict) or e.get("event_id") not in sessions:
            return False, f"unknown event_id {e.get('event_id') if isinstance(e, dict) else e!r}"
        act = e.get("action")
        if act == "shrink" and not (isinstance(e.get("minutes"), (int, float)) and e["minutes"] >= 15):
            return False, f"shrink of {e['event_id']} needs minutes >= 15"
        if act == "move":
            try:
                start = _dt(str(e.get("start", "")))
            except ValueError:
                return False, f"move of {e['event_id']} needs start as YYYY-MM-DDTHH:MM"
            row = sessions[e["event_id"]]
            mins = int(e.get("minutes") or (_dt(row["end"]) - _dt(row["start"])).seconds // 60)
            end = start + timedelta(minutes=mins)
            if not any(w.start <= start and end <= w.start + timedelta(minutes=w.max_minutes)
                       for w in windows):
                return False, (f"{start:%a %d %b %H:%M}-{end:%H:%M} is not inside a free "
                               "window — pick a start from the free windows list")
        elif act not in ("shrink", "delete"):
            return False, f"bad action {act!r} (move|shrink|delete)"
    return True, ""


def edit_by_request(user: str, request: str) -> tuple[dict, object]:
    """Change EXISTING sessions from a free-form request ("push tonight's session to
    tomorrow morning", "cancel Friday", "make it 30 min"). The model reads the
    sessions, free windows and the clock and proposes edits; code verifies them and
    applies each via edit_session. Ambiguity comes back as a question, never a guess.
    Returns ({"narrative", "moves", "question"}, receipt)."""
    now = datetime.now()
    sessions = {e["id"]: e for e in _editable(user)}
    if not sessions:
        return {"narrative": "", "moves": [],
                "question": "You have no upcoming Recal study sessions to change — "
                            "want me to plan one?"}, None
    windows = free_slots(user, min_minutes=15)
    listing = [{"event_id": e["id"], "title": e["title"], "topic": e.get("topic", ""),
                "start": e["start"][:16], "end": e["end"][:16], "status": e["status"]}
               for e in sessions.values()]
    free = [{"from": w.start.strftime("%Y-%m-%dT%H:%M"),
             "to": (w.start + timedelta(minutes=w.max_minutes)).strftime("%Y-%m-%dT%H:%M"),
             "day": w.day} for w in windows]
    prompt = prompt_loader.get(
        "session_edit", request=request, now=now.strftime("%A %Y-%m-%dT%H:%M"),
        sessions=json.dumps(listing, indent=1), free=json.dumps(free, indent=1),
        windows=", ".join(f"{k} {a:%H:%M}-{b:%H:%M}" for k, (a, b) in WINDOWS.items()))
    fallback = {"edits": [], "narrative": "",
                "question": "Which session should I change, and to when?"}
    out, receipt = route("session_edit", prompt, mock_output=fallback,
                         verifier=lambda o: _edit_check(o, sessions, user), domain="edit")
    if not receipt.verified:
        out = fallback
    applied, failed = [], []
    for e in out["edits"]:
        row = sessions[e["event_id"]]
        res = edit_session(user, e["event_id"], e["action"], start=e.get("start"),
                           minutes=e.get("minutes"))
        if not res["ok"]:
            failed.append(f"{row['title']}: {res['reason']}")
            continue
        when = (f"→ {_dt(e['start']):%a %d %b %H:%M}" if e["action"] == "move"
                else f"→ {int(e['minutes'])} min" if e["action"] == "shrink" else "removed")
        applied.append({"event_id": e["event_id"],
                        "action": "drop" if e["action"] == "delete" else e["action"],
                        "minutes": int(e["minutes"]) if e.get("minutes") else None,
                        "reason": f"{row['title']} {when}"
                                  + (f" — {e['reason']}" if e.get("reason") else "")})
    narrative = str(out.get("narrative", "")).strip()
    if failed:
        narrative += (" " if narrative else "") + "Couldn't apply: " + "; ".join(failed) + "."
    return {"narrative": narrative, "moves": applied,
            "question": str(out.get("question", "")).strip() if not applied else ""}, receipt


def mark_done(user: str, event_id: str):
    set_status(user, event_id, "done")
    # TODO(P3): graph.set_mastery + schedule review into a 25-min slot (intervals 1,3,7,16,35)
