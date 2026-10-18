"""LLM-driven request planning — the planner agent.

Every chat message goes to one fast planner call that decomposes it into ordered,
executable steps for the chains in chat_router — resolving pronouns, compound
requests and any stated hours/deadlines inside that single model call. There is
no keyword routing and no regex extraction anywhere: when the planner model is
unreachable (or in MOCK mode) the whole message is handed to the concierge
agent as one "chat" step, and the agent's tools take it from there."""
from __future__ import annotations

from datetime import date, datetime

from ..helper import prompt_loader
from ..helper.config import settings
from . import memory

ACTIONS = {"answer", "material", "reel", "plan", "reschedule", "ingest", "chat"}


def _chat_step(message: str) -> list[dict]:
    """No plan available: the concierge agent handles the whole message with its tools."""
    return [{"action": "chat", "topic": message}]


def _session_day(value) -> str:
    """The planner's on_date if it is a real day from today to two weeks out, else ""."""
    try:
        day = date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return ""
    return day.isoformat() if 0 <= (day - date.today()).days <= 14 else ""


def plan_steps(user: str, message: str, conv: str = "main") -> tuple[list[dict], str]:
    """(steps, origin): origin is 'llm', 'mock' or 'agent'. Each step:
    {action, topic, hours?, deadline_days?, url?} — executed in order by chat_router.
    'agent' means the planner model was unreachable or returned nothing usable,
    so the concierge agent gets the whole message."""
    if settings.MOCK:
        return _chat_step(message), "mock"
    msgs, _ = memory.get_history(user, n=8, conv=conv)
    convo = "\n".join(f"{m.role}: {m.content[:300]}" for m in msgs if m.content) or "(empty)"
    try:
        from .llm import route
        out, _ = route("intent",
                       prompt_loader.get("router_plan", message=message, conversation=convo,
                                         today=datetime.now().strftime("%A %Y-%m-%d")),
                       mock_output={"steps": _chat_step(message)})
    except Exception:  # noqa: BLE001 — a dead planner model must never kill a turn
        return _chat_step(message), "agent"
    steps = []
    for s in out.get("steps", []) if isinstance(out.get("steps"), list) else []:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action", "")).lower().strip()
        if action not in ACTIONS:
            continue
        step = {"action": action, "topic": " ".join(str(s.get("topic", "")).split())[:120]}
        if s.get("url"):
            step["url"] = str(s["url"])
        if action == "reschedule":   # the scheduling agent reads the full instruction
            step["request"] = " ".join(str(s.get("request") or message).split())[:400]
            step["topic"] = step["topic"] or step["request"][:120]
        prefer = str(s.get("prefer", "")).lower().strip()
        step["prefer"] = prefer if prefer in ("morning", "afternoon", "evening") else ""
        try:
            step["hours"] = float(s["hours"]) if s.get("hours") else None
        except (TypeError, ValueError):
            step["hours"] = None
        try:                        # 0 is a real answer ("today"), not "unstated"
            step["deadline_days"] = (max(0, min(int(s["deadline_days"]), 30))
                                     if s.get("deadline_days") not in (None, "") else None)
        except (TypeError, ValueError):
            step["deadline_days"] = None
        step["on_date"] = _session_day(s.get("on_date"))
        steps.append(step)
    return (steps[:5], "llm") if steps else (_chat_step(message), "agent")
