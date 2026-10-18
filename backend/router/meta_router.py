"""Feedback (pedagogy memory), study-material progress, model info and /health."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..features import llm, memory
from ..helper import diagnostics
from ..schemas import FeedbackRequest, FeedbackResponse

router = APIRouter(prefix="/api")


def current_user(request: Request) -> str:
    return request.cookies.get("recal_user") or "demo"


@router.post("/feedback")
def feedback(body: FeedbackRequest, request: Request) -> FeedbackResponse:
    desc = memory.feedback(current_user(request), body.generation_id, body.signal, body.note)
    return FeedbackResponse(ok=True, style_descriptor=desc)


@router.post("/material/progress")
def material_progress(body: dict, request: Request):
    """Managed study session: the frontend reports the furthest page the learner
    reached, so the system always knows how much material is left. Body:
    {topic, page, total}. Returns the stored state {topic, done, total, gen_id}."""
    from ..features import material
    topic = " ".join(str(body.get("topic", "")).split())[:60]
    if not topic:
        return {"ok": False, "reason": "topic required"}
    state = material.advance_progress(current_user(request), topic,
                                      int(body.get("page", 0) or 0),
                                      int(body.get("total", 0) or 0))
    return {"ok": True, **state}


@router.get("/material/progress")
def material_progress_get(topic: str, request: Request):
    state = None
    if topic:
        from ..features import material
        state = material.get_progress(current_user(request), topic)
    return {"ok": bool(state), **(state or {})}


@router.get("/health")
def health(request: Request, live: bool = False):
    """Config + per-feature readiness. ?live=1 makes one real call per model."""
    return diagnostics.report(current_user(request), live=live)


# ---- model info (read-only: models and credentials are server config) ----
@router.get("/settings/llm")
def llm_settings():
    return {"ok": True, **llm.llm_status()}
