"""Mock client meeting: a live, voice-driven dry run with an AI stakeholder.

The browser supplies the ears and the voice (SpeechRecognition + speechSynthesis —
zero model audio quota). This module is the client's BRAIN, and for document
meetings it STUDIES the upload first: one prep pass splits the material into what
the client understood and what it couldn't follow. Those unclear points become the
meeting agenda — the client questions them one by one, judges whether the learner's
explanation actually resolved each, and the final debrief grades the explanations.
route() guarantees graceful degradation: with no model, a heuristic agenda
(unexplained acronyms, numbers without baselines) keeps the meeting alive."""
from __future__ import annotations

import re

from . import documents
from .knowledge import get_graph
from .llm import route
from ..helper import prompt_loader

_meetings: dict[str, dict] = {}   # user -> {topic, context, turns, grasp, agenda}

_CANNED = [
    "Walk me through the core idea once more — slower, and start with why I should care.",
    "What's the strongest objection someone could raise against this, and your answer to it?",
    "Give me one concrete number or example that backs what you just said.",
    "If this goes wrong in the first month, what breaks first?",
    "What would you need from us to make this real?",
]


def _context_for(user: str, topic: str, doc_id: str) -> tuple[str, str, dict | None]:
    """(topic, grounding context, doc or None)."""
    if doc_id:
        doc = documents.get_doc(user, doc_id)
        ctx = "\n\n".join(documents.section_text(s)[:800] for s in doc["sections"][:10])
        return topic or doc["name"], ctx[:4000], doc
    chunks = get_graph(user).relevant_chunks(topic or "the topic", k=6)
    return (topic or "your topic",
            "\n".join(c["text"][:600] for c in chunks)[:4000] or "(no material captured)",
            None)


def _heuristic_agenda(doc: dict) -> tuple[str, list[dict]]:
    """The deterministic reading notes: what trips every real reader — unexplained
    acronyms, bare numbers, generic headers. Used in mock mode and as route()'s
    fallback when the prep model call fails."""
    grasp = (f"the overall storyline of “{doc['name']}” across its "
             f"{len(doc['sections'])} parts")
    items, seen = [], set()
    for s in doc["sections"][:10]:
        where = (s.get("title") or f"part {s.get('n', '?')}")[:60]
        text = documents.section_text(s)
        for a in sorted(set(re.findall(r"\b[A-Z]{2,5}\d?\b", text)))[:2]:
            if a.lower() not in seen:
                seen.add(a.lower())
                items.append({"where": where, "point": f"the term “{a}”",
                              "why": "it's never spelled out"})
        m = documents._NUMBER.search(text or "")
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            items.append({"where": where, "point": f"the figure {m.group(0)}",
                          "why": "there's no baseline or unit context for it"})
    if not items:
        items = [{"where": "the whole document", "point": "the single decision being asked for",
                  "why": "no explicit ask or next step is stated"}]
    return grasp, items[:6]


def _notes(m: dict) -> tuple[str, str]:
    """(open agenda, clarified agenda) rendered for the prompts."""
    open_ = "\n".join(f"- [{a['where']}] {a['point']} — {a['why']}"
                      for a in m["agenda"] if a["status"] == "open") or "none left"
    done = "\n".join(f"- {a['point']} — they explained: {a['answer'][:150]}"
                     for a in m["agenda"] if a["status"] == "resolved") or "none yet"
    return open_, done


def _current(m: dict) -> dict | None:
    return next((a for a in m["agenda"] if a["status"] == "open"), None)


def start(user: str, topic: str = "", doc_id: str = "") -> dict:
    topic, ctx, doc = _context_for(user, topic, doc_id)
    agenda: list[dict] = []
    grasp = f"the gist of {topic}"
    if doc:                                   # the client studies the document first
        h_grasp, h_items = _heuristic_agenda(doc)
        prep, _ = route("ask", prompt_loader.get(
            "meeting_prep", doc=doc["name"], content=ctx),
            mock_output={"grasp": h_grasp, "unclear": h_items}, domain="meeting")
        grasp = str(prep.get("grasp") or h_grasp)[:300]
        raw = prep.get("unclear") if isinstance(prep.get("unclear"), list) else h_items
        agenda = [{"where": str(i.get("where", ""))[:60], "point": str(i.get("point", ""))[:160],
                   "why": str(i.get("why", ""))[:160], "status": "open", "answer": ""}
                  for i in raw if isinstance(i, dict) and i.get("point")][:6] or \
                 [dict(h, status="open", answer="") for h in h_items]
    _meetings[user] = {"topic": topic, "context": ctx, "turns": [],
                       "grasp": grasp, "agenda": agenda}
    m = _meetings[user]
    cur = _current(m)
    opening = (f"Thanks for making time — I read it. Broadly it tracks: {grasp}. "
               f"But one thing I couldn't follow: {cur['point']} in {cur['where']} — "
               f"{cur['why']}. Walk me through that first?"
               if cur else
               f"Thanks for making time. Give me the thirty-second version of {topic} — "
               "what are we deciding today?")
    open_notes, done_notes = _notes(m)
    out, _ = route("ask", prompt_loader.get(
        "meeting_turn", topic=topic, context=ctx, grasp=grasp,
        agenda_open=open_notes, agenda_done=done_notes,
        transcript="(the meeting just started)",
        said="(nothing yet — open the meeting; if you have unclear points, start with the first)"),
        mock_output={"say": opening, "gap": "", "resolved": False}, domain="meeting")
    say = str(out.get("say") or opening)[:400]
    m["turns"].append({"who": "client", "text": say})
    return {"ok": True, "topic": topic, "say": say,
            "open_points": sum(1 for a in agenda if a["status"] == "open")}


def _canned_turn(m: dict, said: str) -> dict:
    """Deterministic client: resolves an agenda item on a substantive explanation
    (>= 15 words), re-asks on a thin one, falls back to the question bank."""
    cur = _current(m)
    if cur:
        if len(said.split()) >= 15:
            nxt = next((a for a in m["agenda"] if a["status"] == "open" and a is not cur), None)
            follow = (f"Next on my list: {nxt['point']} in {nxt['where']} — {nxt['why']}."
                      if nxt else "That was my last open question — anything you want to add?")
            return {"say": f"Alright, that clears up {cur['point']} for me. {follow}",
                    "gap": "", "resolved": True}
        return {"say": f"I'm still not following {cur['point']} — {cur['why']}. "
                       "Try it once more, simpler, as if I'm new to this.",
                "gap": cur["point"], "resolved": False}
    return {"say": _CANNED[(len(m["turns"]) // 2) % len(_CANNED)], "gap": "", "resolved": False}


def turn(user: str, said: str) -> dict:
    m = _meetings.get(user)
    if m is None:
        start(user)
        m = _meetings[user]
    said = said[:1500]
    m["turns"].append({"who": "you", "text": said})
    cur = _current(m)
    if cur:                                   # their words become the explanation on record
        cur["answer"] = (cur["answer"] + " " + said).strip()[:600]
    open_notes, done_notes = _notes(m)
    out, _ = route("ask", prompt_loader.get(
        "meeting_turn", topic=m["topic"], context=m["context"], grasp=m["grasp"],
        agenda_open=open_notes, agenda_done=done_notes,
        transcript=_transcript(m), said=said),
        mock_output=_canned_turn(m, said), domain="meeting")
    if cur and bool(out.get("resolved")):
        cur["status"] = "resolved"
    say = str(out.get("say") or _canned_turn(m, said)["say"])[:400]
    m["turns"].append({"who": "client", "text": say})
    return {"ok": True, "say": say, "gap": str(out.get("gap", ""))[:200],
            "turns": len(m["turns"]),
            "remaining": sum(1 for a in m["agenda"] if a["status"] == "open")}


def _transcript(m: dict, n: int = 10) -> str:
    return "\n".join(f"{t['who']}: {t['text'][:300]}" for t in m["turns"][-n:]) or "(start)"


def finish(user: str) -> dict:
    m = _meetings.pop(user, None)
    if not m or not m["turns"]:
        return {"ok": True, "summary": "No meeting in progress."}
    resolved = [a for a in m["agenda"] if a["status"] == "resolved"]
    open_ = [a for a in m["agenda"] if a["status"] == "open"]
    agenda_report = (
        "\n".join(f"- CLARIFIED: {a['point']} — their explanation: {a['answer'][:200]}" for a in resolved)
        + ("\n" if resolved and open_ else "")
        + "\n".join(f"- STILL UNCLEAR: {a['point']} ({a['why']})" for a in open_)) or "(no agenda — open-topic meeting)"
    mock = ("### How the meeting went\nYou held the room and worked through the list.\n\n"
            "**You cleared up**\n"
            + ("\n".join(f"- {a['point']}" for a in resolved) or "- (nothing yet)")
            + "\n\n**Still unclear to the client**\n"
            + ("\n".join(f"- {a['point']} — {a['why']}" for a in open_) or "- nothing — clean sweep")
            + "\n\n**Drill this next:** have one number and one plain-words definition "
              "ready for every claim — that's where clients push.")
    out, _ = route("ask", prompt_loader.get(
        "meeting_summary", topic=m["topic"], transcript=_transcript(m, 24),
        agenda=agenda_report),
        mock_output={"summary": mock}, domain="meeting")
    return {"ok": True, "summary": str(out.get("summary") or mock)[:4000],
            "turns": len(m["turns"]),
            "cleared": len(resolved), "unresolved": len(open_)}
