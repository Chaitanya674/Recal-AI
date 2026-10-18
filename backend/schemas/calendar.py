from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from .common import Citation

class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    kind: Literal["work", "study", "review", "recovery"]
    status: Literal["planned", "done", "missed"] = "planned"
    topic: str = ""
    resources: list[Citation] = []
    moved_from: datetime | None = None
    why: str = ""

class Slot(BaseModel):
    slot_id: str           # "S0", "S1"... the ONLY placement targets the model may use
    start: datetime
    max_minutes: int
    day: str

class Move(BaseModel):
    event_id: str
    action: Literal["move", "shrink", "drop"]
    to_slot_id: str | None = None
    minutes: int | None = None
    reason: str = ""       # <=120 chars, human-readable "why"
