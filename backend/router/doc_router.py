"""Documents: upload (PPTX/DOCX/XLSX), review (message / client-ready), rehearse coach.

Reviews come back as Card JSON and are also saved to conversation memory, so the
frontend can drop them straight into the Cards tab and they survive a page reload."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from ..features import documents, memory, rehearse
from ..helper import guardrails
from ..schemas import Card
from .chat_router import current_user

router = APIRouter(prefix="/api/docs")

TITLES = {"message": "Message check", "client_ready": "Client-ready check"}


def _err(e: documents.DocError):
    return JSONResponse({"ok": False, "reason": e.reason}, status_code=e.status)


@router.get("")
def list_docs(request: Request):
    return {"ok": True, "docs": documents.list_docs(current_user(request))}


@router.post("")
async def upload(request: Request, file: UploadFile = File(...)):
    data = await file.read()
    try:
        doc = documents.parse_upload(current_user(request), file.filename or "upload", data)
    except documents.DocError as e:
        return _err(e)
    return {"ok": True, "doc": {"id": doc["id"], "name": doc["name"], "kind": doc["kind"],
                                "sections": len(doc["sections"])}}


@router.get("/{doc_id}")
def get_doc(doc_id: str, request: Request):
    try:
        doc = documents.get_doc(current_user(request), doc_id)
    except documents.DocError as e:
        return _err(e)
    return {"ok": True, "doc": {k: doc[k] for k in ("id", "name", "kind", "sections")}}


@router.post("/{doc_id}/review")
def review(doc_id: str, body: dict, request: Request):
    user = current_user(request)
    mode = body.get("mode", "message")
    try:
        payload, receipt = documents.review(user, doc_id, mode)
    except documents.DocError as e:
        return _err(e)
    card = Card(id=uuid.uuid4().hex[:12], type="doc_review",
                title=f"{TITLES.get(mode, 'Review')}: {payload['doc_name'][:50]}",
                receipt=receipt, payload=payload)
    memory.save_card(user, card)
    memory.append_message(user, "assistant", content=f"[{card.title}] {payload['headline']}",
                          card_ids=[card.id], receipt=receipt)
    return {"ok": True, "card": guardrails.redact_obj(card.model_dump(mode="json"))}


@router.post("/{doc_id}/coach")
async def coach(doc_id: str, request: Request, section: int = Form(...),
                audio: UploadFile | None = File(None)):
    user = current_user(request)
    blob = await audio.read() if audio else None
    try:
        out, receipt = rehearse.coach(user, doc_id, section, blob,
                                      mime=(audio.content_type if audio else ""))
    except documents.DocError as e:
        return _err(e)
    return {"ok": True, **guardrails.redact_obj(out),
            "receipt": {"model": receipt.model, "verified": receipt.verified}}


@router.delete("/{doc_id}/coach")
def coach_reset(doc_id: str, request: Request):
    rehearse.reset(current_user(request), doc_id)
    return {"ok": True}
 