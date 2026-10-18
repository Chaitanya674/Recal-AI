"""Shared primitives used across features."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel

class Receipt(BaseModel):
    model: str
    thinking: Literal["none", "low", "high"] = "low"
    verified: bool = True
    escalated: bool = False
    ms: int = 0
    reason: str = ""

class Citation(BaseModel):
    chunk_id: str
    url: str = ""
    title: str = ""
