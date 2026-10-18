"""Endpoint 1: POST /api/chat — the single brain door (SSE).

Routing is fully agentic (features/intents.plan_steps): one planner-agent call
decomposes the message into ordered steps, each executed by a verified chain
below. There is no keyword routing: anything the planner can't map — or any
turn where the planner model is unreachable — goes to the concierge agent,
which works through its tools.
Event ordering per plan §3.2: (status|token|card)* -> receipt* -> state? -> done.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..agents import workflow
from ..features import calendar as cal
from ..features import google, intents, knowledge, material, memory, motion
from ..features.llm import chat_stream, model_id
from ..helper import guardrails
from ..helper.config import settings
from ..schemas import Card, ChatRequest, Connections, Receipt

router = APIRouter(prefix="/api")


def current_user(request: Request) -> str:
    return request.cookies.get("recal_user") or "demo"


def current_conv(request: Request) -> str:
    return request.cookies.get("recal_conv") or "main"


@router.get("/conversations")
def conversations(request: Request):
    """Session sidebar: [{id, title, ts}] newest first."""
    return {"conversations": memory.list_conversations(current_user(request))}


@router.post("/conversations")
def new_conversation(request: Request):
    """Start a fresh chat session; the frontend stores the id in the recal_conv cookie."""
    return {"id": memory.new_conversation(current_user(request))}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {guardrails.redact(json.dumps(data, default=str))}\n\n"


def _card(type_: str, title: str, payload: dict, receipt: Receipt) -> Card:
    return Card(id=uuid.uuid4().hex[:12], type=type_, title=title,
                receipt=receipt, payload=payload)


def _state(user: str, with_history: bool = False, conv: str = "main") -> dict:
    msgs, cards = memory.get_history(user, conv=conv) if with_history else ([], [])
    return {"week": cal.week(user),
            "graph": knowledge.get_graph(user).stats(),
            "connections": Connections(calendar=google.connected(user, "calendar"),
                                       gdocs=google.connected(user, "gdocs")).model_dump(),
            "mock": settings.MOCK,
            "history": ({"messages": [m.model_dump(mode="json") for m in msgs],
                         "cards": [c.card.model_dump(mode="json") for c in cards]}
                        if with_history else None)}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request):
    user = current_user(request)
    conv = current_conv(request)

    async def stream():
        card_ids: list[str] = []
        prose: list[str] = []
        receipt: Receipt | None = None
        last_receipt: Receipt | None = None

        def emit_card(c: Card):
            memory.save_card(user, c)
            card_ids.append(c.id)
            return _sse("card", c.model_dump(mode="json"))

        try:
            memory.append_message(user, "user", content=body.message or "",
                                  action=body.action, conv=conv)
            try:                                              # Google Calendar mirror (60s TTL)
                cal.sync_google(user)
            except google.GoogleError as e:
                yield _sse("status", {"text": f"Google Calendar sync failed: {e.reason}"})

            if body.action:                                   # ---- direct dispatch ----
                a = body.action
                if a.type == "bootstrap":
                    yield _sse("state", _state(user, with_history=True, conv=conv))
                    yield _sse("done", {})
                    return
                if a.type == "reset":
                    memory.reset_user(user, conv=conv)
                    cal.reset_week(user)
                    yield _sse("status", {"text": "Reset: chat, cards and Recal's study sessions cleared."})
                elif a.type == "mark_status":
                    cal.set_status(user, a.event_id, a.status)
                    if a.status == "done":
                        cal.mark_done(user, a.event_id)
                        # Managed session contract: session over + pages pending ->
                        # offer to book another session to finish the material.
                        ev = next((e for e in cal.week(user) if e["id"] == a.event_id), None)
                        prog = material.get_progress(user, (ev or {}).get("topic") or "")
                        if prog and prog["done"] < prog["total"]:
                            left = prog["total"] - prog["done"]
                            yield _sse("status", {
                                "text": f'Session done, but "{prog["topic"]}" still has '
                                        f'{left} of {prog["total"]} pages left — say '
                                        f'"plan another session for {prog["topic"]}" and '
                                        "I'll block time to finish it."})
                    yield _sse("status", {"text": f"Marked {a.status}."})
                elif a.type == "urgent":
                    cal.add_event(user, a.title or "Urgent work",
                                  a.start or datetime.now() + timedelta(days=1, hours=2),
                                  a.minutes or 120, "work", why="urgent")
                    out, receipt = cal.replan(user)
                    yield emit_card(_card("calendar_diff",
                                          "Renegotiated around urgent work", out, receipt))
                elif a.type in ("replan", "energy"):
                    out, receipt = cal.replan(user, energy=a.level or "normal")
                    yield emit_card(_card("calendar_diff", "Tonight's renegotiation",
                                          out, receipt))
                elif a.type == "export":
                    rec = memory.get_card(user, a.card_id or "")
                    if not rec:
                        yield _sse("status", {"text": "Card not found."})
                    elif not google.connected(user, "gdocs"):
                        yield _sse("status", {"text": "Connect Google Docs first (Google Docs pill)."})
                    else:
                        yield _sse("status", {"text": "Creating a Google Doc…"})
                        url = google.export_card(user, rec.card)
                        yield _sse("status", {"text": f"Exported to Google Docs: {url}"})

            else:                                             # ---- message ----
                async def handle(step: dict):
                    """One planner step's chain. The planner agent already resolved
                    topics, pronouns and any stated hours/deadlines."""
                    nonlocal receipt
                    action = step.get("action", "chat")
                    topic = step.get("topic") or (body.message or "")

                    if action == "ingest":
                        url = step.get("url") or topic
                        payload = knowledge.ingest_url(user, url)
                        receipt = Receipt(model="pipeline", verified=True)
                        yield emit_card(_card("source", payload["title"], payload, receipt))
                        prose.append("Captured into your graph — ask me anything about it.")
                        yield _sse("token", {"delta": prose[-1]})
                        return

                    if action == "plan":
                        yield _sse("status", {"text": "Reading your free slots…"})
                        kw = {k: step[k] for k in ("hours", "prefer", "on_date") if step.get(k)}
                        if step.get("deadline_days") is not None:     # 0 = today, a real value
                            kw["deadline_days"] = step["deadline_days"]
                        payload, receipt = cal.plan(user, topic, **kw)
                        yield emit_card(_card("plan", f"Plan: {topic[:48]}", payload, receipt))
                        # the Week tab updates now, not after the rest of a compound turn
                        yield _sse("state", _state(user, conv=conv))
                        # Chat gets a one-line pointer only — the plan card holds the
                        # details (a free-form model call here used to dump essays).
                        sessions = payload.get("sessions") or []
                        mins = sum(s.get("minutes", 0) for s in sessions)
                        days = " and ".join(dict.fromkeys(
                            datetime.fromisoformat(str(s["start"])).strftime("%a %d %b")
                            for s in sessions))
                        prose.append(
                            f'Blocked {len(sessions)} session'
                            f'{"s" if len(sessions) != 1 else ""} ({mins} min) on {days} for '
                            f'"{topic}" — details on the plan card.' if sessions else
                            f'No free slot fit "{topic}" — {payload.get("summary", "see the plan card")}.')
                        yield _sse("token", {"delta": prose[-1]})
                        return

                    if action == "reschedule":
                        yield _sse("status", {"text": "Scheduling agent: reading your sessions…"})
                        out, rec = await asyncio.to_thread(
                            cal.edit_by_request, user, step.get("request") or topic)
                        if out["moves"]:
                            receipt = rec
                            yield emit_card(_card("calendar_diff", "Sessions updated",
                                                  {"narrative": out["narrative"],
                                                   "moves": out["moves"]}, receipt))
                            yield _sse("state", _state(user, conv=conv))
                            prose.append(out["narrative"] or "Done — see the updated sessions card.")
                        else:
                            receipt = rec or Receipt(model="pipeline", verified=True)
                            prose.append(out["question"] or out["narrative"]
                                         or "I couldn't apply that change — which session, and to when?")
                        yield _sse("token", {"delta": prose[-1]})
                        return

                    if action == "reel":
                        yield _sse("status", {"text": f'Motion agent: focusing "{topic}" on your ask, directing visuals…'})
                        payload, receipt = await asyncio.to_thread(
                            motion.make_reel, user, topic, body.message or "")
                        if payload is None:          # nothing captured: find sources, retry once
                            yield _sse("status", {"text": f'Nothing captured on "{topic}" — searching the web…'})
                            urls, problem = await asyncio.to_thread(knowledge.find_sources, topic)
                            if problem:               # network/TLS trouble must never be silent
                                yield _sse("status", {"text": f"Web search failed: {problem}"})
                            got, ingest_err = 0, ""
                            for url in urls:          # comparative topics get one source per side
                                yield _sse("status", {"text": f"Reading {url} …"})
                                try:
                                    src = await asyncio.to_thread(knowledge.ingest_url, user, url)
                                    yield emit_card(_card("source", src["title"], src,
                                                          Receipt(model="pipeline", verified=True)))
                                    got += 1
                                except Exception as e:  # noqa: BLE001 — loud, then carry on
                                    ingest_err = f"{type(e).__name__}: {str(e)[:120]}"
                                    yield _sse("status", {"text": f"Couldn't read {url}: {ingest_err}"})
                            if problem or not got:    # search degraded: the model itself is a source
                                yield _sse("status", {"text": "Search is limited — adding the model's "
                                                              "own knowledge as a source…"})
                                src = await asyncio.to_thread(
                                    knowledge.model_knowledge_source, user, topic)
                                if src:
                                    yield emit_card(_card("source", src["title"], src,
                                                          Receipt(model="pipeline", verified=True)))
                                    got += 1
                            if got:
                                payload, receipt = await asyncio.to_thread(
                                    motion.make_reel, user, topic, body.message or "")
                        if payload is None:
                            prose.append(
                                f"My web search for \"{topic}\" failed ({problem}) — the server "
                                "may be blocked from reaching the internet (see /api/health?live=1). "
                                "Paste a docs URL instead."
                                if problem and not urls else
                                f"I found sources on \"{topic}\" but couldn't ingest them "
                                f"({ingest_err}) — usually model quota; try again in a bit."
                                if urls and not got and ingest_err else
                                f"I couldn't find a usable source on \"{topic}\". "
                                "Paste a docs URL on it, then ask me to explain it visually.")
                        else:
                            if not settings.MOCK and payload["narration"] != "gemini-tts":
                                yield _sse("status", {"text": f"Narration: {payload['narration']}"})
                            reason = str(getattr(receipt, "reason", ""))
                            if reason.startswith("extractive"):
                                detail = reason.removeprefix("extractive fallback").strip(" (").rstrip(")")
                                yield _sse("status", {"text": "This reel is built directly from source "
                                                              f"sentences — {detail or 'script model failed'}."})
                            yield emit_card(_card("reel", payload["scene_script"].get("title", "Reel"),
                                                  payload, receipt))
                            prose.append("Reel is on the canvas — every scene cites a source. "
                                         "Download it as a video or slides from the Slides tab.")
                        yield _sse("token", {"delta": prose[-1]})
                        return

                    if action == "material":
                        yield _sse("status", {"text": f'Material drafter: writing "{topic}" from grounded chunks…'})
                        payload, receipt = await asyncio.to_thread(
                            material.draft_material, user, topic, "", session_minutes)
                        if payload is None:          # nothing captured: find sources, retry once
                            yield _sse("status", {"text": f'Nothing captured on "{topic}" — searching the web…'})
                            urls, problem = await asyncio.to_thread(knowledge.find_sources, topic)
                            if problem:
                                yield _sse("status", {"text": f"Web search failed: {problem}"})
                            got, ingest_err = 0, ""
                            for url in urls:
                                yield _sse("status", {"text": f"Reading {url} …"})
                                try:
                                    src = await asyncio.to_thread(knowledge.ingest_url, user, url)
                                    yield emit_card(_card("source", src["title"], src,
                                                          Receipt(model="pipeline", verified=True)))
                                    got += 1
                                except Exception as e:  # noqa: BLE001 — loud, then carry on
                                    ingest_err = f"{type(e).__name__}: {str(e)[:120]}"
                                    yield _sse("status", {"text": f"Couldn't read {url}: {ingest_err}"})
                            if problem or not got:    # search degraded: the model itself is a source
                                yield _sse("status", {"text": "Search is limited — adding the model's "
                                                              "own knowledge as a source…"})
                                src = await asyncio.to_thread(
                                    knowledge.model_knowledge_source, user, topic)
                                if src:
                                    yield emit_card(_card("source", src["title"], src,
                                                          Receipt(model="pipeline", verified=True)))
                                    got += 1
                            if got:
                                payload, receipt = await asyncio.to_thread(
                                    material.draft_material, user, topic, "", session_minutes)
                        if payload is None:
                            prose.append(
                                f"I found sources on \"{topic}\" but couldn't ingest them "
                                f"({ingest_err}) — usually model quota; try again in a bit."
                                if urls and not got and ingest_err else
                                f"I couldn't find a usable source on \"{topic}\". "
                                "Paste a docs URL on it, then ask me for notes again.")
                            yield _sse("token", {"delta": prose[-1]})
                        else:
                            # critic loop (2 passes MAX): reviews alignment, grounding
                            # and the learner's taste; 'major' triggers one revision
                            yield _sse("status", {"text": "Critic reviewing the draft…"})
                            payload, receipt, critic_note = await asyncio.to_thread(
                                material.review_and_revise, user, body.message or topic,
                                topic, payload, receipt, session_minutes)
                            if critic_note:
                                yield _sse("status", {"text": critic_note})
                            yield emit_card(_card("page", payload["title"], payload, receipt))
                            prose.append("Notes are on the canvas — every claim cites a source.")
                            yield _sse("token", {"delta": prose[-1]})
                        return

                    if action == "answer":
                        res = knowledge.ask(user, topic)
                        if res["from"] == "graph":
                            yield _sse("status", {"text": "Answering from your graph…"})
                            source = (("Grounded answer (mock): "
                                       + res["answer_context"][:220]).split(" ")
                                      if settings.MOCK else chat_stream(res["answer_context"]))
                            for d in source:
                                delta = d + " " if settings.MOCK else d
                                prose.append(delta)
                                yield _sse("token", {"delta": delta})
                                await asyncio.sleep(0)
                            receipt = Receipt(model=model_id("fast"), verified=True,
                                              reason=f"{len(res['citations'])} citations")
                        else:
                            yield _sse("status", {"text": "Nothing captured yet — searching live…"})
                            for d in chat_stream(topic, ground=True):
                                prose.append(d)
                                yield _sse("token", {"delta": d})
                                await asyncio.sleep(0)
                            receipt = Receipt(model=model_id("fast"), reason="live")
                        return

                    # ---- "chat" -> ADK concierge / direct model ----
                    yield _sse("status", {"text": "Concierge agent thinking…"})
                    text = await asyncio.to_thread(workflow.run_freeform, user, topic)
                    prose.append(text)
                    yield _sse("token", {"delta": text})
                    receipt = Receipt(model="adk-concierge" if not settings.MOCK
                                      else "mock-concierge", verified=True)

                # the safety guard runs alongside the router: no added latency, and
                # nothing executes until the guard has cleared the message
                guard, (steps, origin) = await asyncio.gather(
                    asyncio.to_thread(guardrails.check_input, body.message or "", "chat"),
                    asyncio.to_thread(intents.plan_steps, user, body.message or "", conv))
                if not guard["allow"]:
                    steps = []
                    prose.append(guard["reply"])
                    yield _sse("token", {"delta": guard["reply"]})
                    receipt = Receipt(model="guardrail", verified=True,
                                      reason=f"blocked: {guard['category']}")
                # A compound "plan 2 hours + material" turn: the material book is
                # sized to the booked study time (one page ≈ 15-20 minutes).
                session_minutes = next((int(s["hours"] * 60) for s in steps
                                        if s.get("hours")), 0)
                if origin == "llm" and len(steps) > 1:
                    yield _sse("status", {"text": "planner: " + " -> ".join(
                        f"{s['action']}({s['topic'][:32]})" for s in steps)})
                for step in steps:
                    yield _sse("status", {"text": f"intent: {step['action']}"})
                    async for ev in handle(step):
                        yield ev
                    if receipt:                   # each request in a compound turn gets
                        yield _sse("receipt", receipt.model_dump())   # its own receipt
                        last_receipt, receipt = receipt, None

            if receipt:
                yield _sse("receipt", receipt.model_dump())
            memory.append_message(user, "assistant", content="".join(prose),
                                  card_ids=card_ids, receipt=receipt or last_receipt,
                                  conv=conv)
            yield _sse("state", _state(user))
            yield _sse("done", {})
        except Exception as e:  # noqa: BLE001 — graceful, never a dropped stream
            yield _sse("error", {"message": str(e)[:200], "recoverable": True})
            yield _sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
