"""Canvas cards — the dynamic output schemas streamed over the chat endpoint."""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Union
from pydantic import BaseModel
from .common import Receipt, Citation
from .reel import SceneScript
from .calendar import Move

class ReelPayload(BaseModel):
    scene_script: SceneScript
    durations: list[float] = []        # seconds per scene (follows the narration audio)
    audio: list[str] | None = None     # per-scene narration URLs, None = captions only
    narration: str = ""                # "gemini-tts" or "captions only (<why>)"
    generation_id: str

class PagePayload(BaseModel):
    markdown: str
    sections: list[dict] = []          # [{title, kind: concept|code|exercise, markdown}]
    citations: list[Citation] = []
    style_used: list[str] = []
    generation_id: str

class PlanSession(BaseModel):
    event_id: str
    title: str
    start: datetime
    minutes: int
    why: str = ""
    resources: list[Citation] = []

class PlanPayload(BaseModel):
    summary: str
    sessions: list[PlanSession]

class SourcePayload(BaseModel):
    url: str
    title: str
    chunks: int
    concepts: int
    linked: int = 0
    prereq_edges: int = 0

class DiffPayload(BaseModel):
    narrative: str
    moves: list[Move]

class ReviewIssue(BaseModel):
    where: str = "document"
    severity: Literal["high", "medium", "low"] = "medium"
    issue: str
    fix: str

class DocReviewPayload(BaseModel):
    doc_id: str
    doc_name: str
    mode: Literal["message", "client_ready"]
    headline: str
    key_message: str = ""
    issues: list[ReviewIssue] = []
    stats: dict = {}

Payload = Union[ReelPayload, PagePayload, PlanPayload, SourcePayload, DiffPayload,
                DocReviewPayload]

class Card(BaseModel):
    id: str
    type: Literal["reel", "page", "plan", "source", "calendar_diff", "doc_review"]
    title: str
    receipt: Receipt
    payload: dict   # one of the payloads above; kept as dict for simple SSE serialization
