"""Conversation persistence (memory.py): messages, conversations, stored cards."""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from .chat import Action
from .common import Receipt
from .card import Card

class Message(BaseModel):
    id: str
    conv_id: str = "main"
    role: Literal["user", "assistant"]
    content: str = ""
    action: Action | None = None
    card_ids: list[str] = []
    receipt: Receipt | None = None
    ts: datetime

class Conversation(BaseModel):
    id: str = "main"
    user_id: str
    title: str = "Workbench"
    summary: str = ""
    msg_count: int = 0

class CardRecord(BaseModel):
    card: Card
    conv_id: str = "main"
    msg_id: str = ""
    ts: datetime
