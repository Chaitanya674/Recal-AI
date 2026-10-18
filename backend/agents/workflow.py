"""Free-form chat: the ADK concierge behind chat_router's "chat" step.

The planner (features/intents) already routes plans, reels, study material,
reschedules and ingests to their verified chains; whatever it can't map lands
here. The concierge answers from the learner's own sources and calendar through
three tools, and points creation requests back at the phrasing the planner routes.

Tools close over the user id, so the model can never address another user's data.
Without ADK (MOCK mode, package missing, Vertex unreachable) the message goes to the
chat model directly — free-form chat never dies on an agent-framework issue.
"""
from __future__ import annotations

import json
from datetime import datetime

from ..features import calendar as cal
from ..features import knowledge
from ..features.llm import available, chat_stream
from ..helper import prompt_loader
from ..helper.guardrails import wrap_untrusted
from ..helper.logger import get_logger
from .agent_factory import AgentFactory

_APP = "recal"
_log = get_logger(__name__)

# The concierge prompt describes a multi-agent graph; this deployment runs the
# specialists as chat_router chains, so creation requests get a routable phrasing.
_NO_ORCHESTRATOR = (
    "\n\n# THIS DEPLOYMENT\nThere is no orchestrator to transfer to. For anything that "
    "creates plans or artifacts (booking study time, a reel, study material, a review), "
    "tell the user the one-line request to send, e.g. \"Plan 2 hours tomorrow evening "
    "for Kafka\" or \"Explain Kafka partitions visually\". Never claim it was done.")


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _tools(user: str) -> list:
    def search_context(question: str) -> dict:
        """Search the learner's captured sources for passages relevant to a question.

        Args:
            question: the learner's question, in their words.

        Returns:
            answerable: whether any captured passage is on-topic.
            passages: [{id, text}] to answer from, citing ids like [id].
        """
        chunks = knowledge.get_graph(user).relevant_chunks(question, k=5)
        return {"answerable": bool(chunks),
                "passages": [{"id": c["id"], "text": wrap_untrusted(c.get("text", "")[:1200])}
                             for c in chunks]}

    def read_week() -> dict:
        """Read this week's calendar: Recal study sessions and mirrored Google events.

        Returns:
            events: [{id, title, start, end, kind, status, topic}].
        """
        return {"events": _jsonable(cal.week(user))}

    def edit_study_session(request: str) -> dict:
        """Move, shrink or delete existing Recal study sessions from a plain request.

        Args:
            request: the change in the user's words, e.g. "move tomorrow's session to 7pm".

        Returns:
            narrative: what changed; moves: applied edits; question: a clarifying
            question when the request was ambiguous (nothing was changed then).
        """
        out, _ = cal.edit_by_request(user, request)
        return _jsonable(out)

    return [search_context, read_week, edit_study_session]


def _concierge(user: str):
    factory = AgentFactory()
    agent = factory.create(
        "concierge", tier="fast", instruction_key="concierge_instruction",
        tools=_tools(user), description="Recal's front door for free-form chat",
        today=datetime.now().strftime("%A %Y-%m-%d"))
    if agent is not None:
        agent.instruction += _NO_ORCHESTRATOR
    return agent


def _direct(message: str) -> str:
    """No agent: the chat model answers under the concierge prompt (canned in MOCK)."""
    if not available():
        return "".join(chat_stream(message))
    return "".join(chat_stream(prompt_loader.get(
        "concierge_instruction", today=datetime.now().strftime("%A %Y-%m-%d"))
        + _NO_ORCHESTRATOR + "\n\nUser: " + message))


def run_freeform(user: str, message: str) -> str:
    """One concierge turn. Blocking — call it from a worker thread."""
    agent = _concierge(user)
    if agent is None:
        return _direct(message)
    try:
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        runner = InMemoryRunner(agent=agent, app_name=_APP)
        session = runner.session_service.create_session_sync(app_name=_APP, user_id=user)
        text = ""
        for event in runner.run(user_id=user, session_id=session.id,
                                new_message=types.Content(role="user",
                                                          parts=[types.Part(text=message)])):
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts
                               if not getattr(p, "thought", False))
        if text.strip():
            return text.strip()
    except Exception:  # noqa: BLE001 — degrade to the plain chat model below
        _log.exception("concierge agent failed; answering with the chat model")
    return _direct(message)
