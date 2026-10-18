"""The single chat endpoint: request body + SSE event envelope.
SSE frame: `event: <type>\\ndata: <json>\\n\\n`.
Ordering guarantee: (status|token|card)* -> receipt* -> state? -> done. Always ends in done."""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class Action(BaseModel):
    type: Literal["bootstrap", "mark_status", "urgent", "energy",
                  "replan", "export", "reset"]
    event_id: str | None = None
    status: Literal["done", "missed"] | None = None
    title: str | None = None
    start: datetime | None = None
    minutes: int | None = None
    level: Literal["tired", "normal"] | None = None
    card_id: str | None = None
    dest: Literal["gdocs"] | None = None

class ChatRequest(BaseModel):
    # hard cap against oversized bodies; the friendly limit lives in guardrails
    message: str | None = Field(default=None, max_length=20000)
    action: Action | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.message) == bool(self.action):
            raise ValueError("provide exactly one of message | action")
        return self
