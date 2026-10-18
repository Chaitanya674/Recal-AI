"""Mock client meeting endpoints: a live, voice-driven dry run. The browser does
STT/TTS; these endpoints drive the client's brain (features/meeting.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..features import meeting
from ..helper import guardrails
from ..helper.identity import current_user

router = APIRouter(prefix="/api/meeting")


@router.post("/start")
def start(body: dict, request: Request):
    """{"topic"?: str, "doc_id"?: str} -> the client's opening question."""
    return guardrails.redact_obj(meeting.start(current_user(request),
                                               topic=str(body.get("topic", ""))[:120],
                                               doc_id=str(body.get("doc_id", ""))))


@router.post("/turn")
def turn(body: dict, request: Request):
    """{"said": str} (the learner's transcribed speech) -> the client's reply."""
    said = str(body.get("said", ""))
    guard = guardrails.check_input(said, "meeting")
    if not guard["allow"]:
        return {"ok": True, "say": guard["reply"], "gap": "", "blocked": guard["category"]}
    return guardrails.redact_obj(meeting.turn(current_user(request), said))


@router.post("/end")
def end(request: Request):
    """Debrief: strengths, gaps a client saw, and what to drill next."""
    return guardrails.redact_obj(meeting.finish(current_user(request)))
