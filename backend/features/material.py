"""Study material drafter: grounded one-page notes from the knowledge graph.

Replaces the old `make_material` placeholder. Same stance as reels (motion.py):
  1. relevant chunks only — nothing on-topic => no page (caller may auto-ingest)
  2. the model writes markdown citing passage ids; a verifier checks grounding
  3. invalid twice => extractive fallback built from the sources, so a page
     always ships (mock mode uses the same builder)
Critic critiques (agent loop, 2 passes max) are injected as revision notes."""
from __future__ import annotations

import json
import re

from .knowledge import get_graph
from .llm import route
from .memory import avoids_for, record_generation, styles_for
from ..database import fire, schema
from ..helper import appconfig, prompt_loader
from ..helper.config import settings
from ..helper.markdown_fix import normalize_citations, repair_math


# ---- study progress (the managed-session contract: the system always knows how
# much of the material is read and how much is left). Firestore: one small doc per
# topic at users/{uid}/progress/{slug}; locally: the app's SQLite config table. -----
def progress_slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60] or "untitled"


def _put_progress(user: str, topic: str, state: dict):
    if fire.enabled():
        fire.set_doc({**state, "meta": fire.meta()}, *schema.progress(user), progress_slug(topic))
    else:
        appconfig.put(f"material_progress:{user}:{progress_slug(topic)}", json.dumps(state))


def set_progress(user: str, topic: str, gen_id: str, done: int, total: int):
    _put_progress(user, topic, {"topic": topic, "gen_id": gen_id,
                                "done": max(0, int(done)), "total": max(1, int(total))})


def get_progress(user: str, topic: str) -> dict | None:
    if fire.enabled():
        try:
            doc = fire.get_doc(*schema.progress(user), progress_slug(topic))
        except Exception:  # noqa: BLE001
            return None
        return {k: v for k, v in doc.items() if k != "meta"} if doc else None
    raw = appconfig.get(f"material_progress:{user}:{progress_slug(topic)}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def advance_progress(user: str, topic: str, page: int, total: int) -> dict:
    """Record the furthest page reached (never regresses). Returns the new state."""
    cur = get_progress(user, topic) or {"topic": topic, "gen_id": "", "done": 0,
                                        "total": max(1, int(total))}
    cur["total"] = max(1, int(total) or cur["total"])
    cur["done"] = min(cur["total"], max(cur["done"], int(page)))
    _put_progress(user, topic, cur)
    return cur

def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(text).strip()) if s.strip()]


def _extractive_page(topic: str, chunks: list[dict]) -> dict:
    """A valid, grounded page built straight from the source text (no model)."""
    sections = []
    for ch in chunks[:4]:
        body = " ".join(_sentences(ch["text"])[:2])
        sections.append(f"## From your sources [{ch['id']}]\n\n{body}")
    md = (f"# {topic}\n\nKey points from {len(chunks)} captured passage"
          f"{'s' if len(chunks) != 1 else ''}.\n\n" + "\n\n".join(sections)
          + "\n\n## Check yourself\n"
            f"1. Explain {topic} in one sentence.\n"
            "2. Which claim above would you verify first, and how?\n"
            "3. Where would you apply this tomorrow?")
    return {"markdown": md, "citations": [c["id"] for c in chunks[:4]]}


def _split_sections(markdown: str) -> list[dict]:
    """Derive slide sections from a combined markdown page (## boundaries)."""
    parts = re.split(r"(?m)^##\s+", markdown)
    sections = []
    for part in parts[1:]:
        head, _, body = part.partition("\n")
        kind = ("exercise" if any(w in head.lower() for w in ("exercise", "yourself", "check"))
                else "code" if "```" in body else "concept")
        sections.append({"title": head.strip(), "kind": kind,
                         "markdown": f"## {head.strip()}\n{body.strip()}"})
    return sections or [{"title": "Overview", "kind": "concept", "markdown": markdown}]


def _build_sections(user: str, title: str, ctx: str, critiques: str,
                    fallback: dict, minutes: int = 0) -> list[dict]:
    """Multi-call generation (the NotebookLM stance): one outline call sizes the
    BOOK to the topic's complexity and the booked study time, then ONE call per
    page (concept / code steps / DIY exercise). Any failed piece degrades to its
    extractive counterpart."""
    fb_sections = _split_sections(fallback["markdown"])
    out, _ = route("page", prompt_loader.get("material_outline", topic=title,
                                                   chunks=ctx, minutes=minutes or "unknown"),
                         mock_output={"sections": [
                             {"title": s["title"], "kind": s["kind"], "focus": ""}
                             for s in fb_sections]},
                         domain=title[:24])
    plan = [s for s in (out.get("sections") or []) if isinstance(s, dict)
            and str(s.get("kind", "")) in ("concept", "code", "exercise")][:8]
    if not plan:
        return fb_sections
    crit_block = (f"# Revision notes from the critic (apply each one)\n{critiques}"
                  if critiques else "")
    sections = []
    for i, sec in enumerate(plan):
        sec_title = str(sec.get("title", f"Part {i + 1}"))[:80]
        mock_sec = fb_sections[min(i, len(fb_sections) - 1)]
        try:
            body, _ = route("page", prompt_loader.get(
                "material_section", topic=title, title=sec_title,
                kind=sec.get("kind", "concept"), focus=str(sec.get("focus", ""))[:200],
                chunks=ctx, styles=styles_for(user, title) or "none yet",
                critiques=crit_block),
                mock_output={"markdown": mock_sec["markdown"]}, domain=title[:24])
            md = str(body.get("markdown", "")).strip()
        except Exception:  # noqa: BLE001 — a failed section gets extractive filler
            md = ""
        if len(md) < 40:
            md = mock_sec["markdown"]
        sections.append({"title": sec_title, "kind": sec.get("kind", "concept"),
                         "markdown": md})
    return sections


def draft_material(user: str, msg: str, critiques: str = "",
                   minutes: int = 0) -> tuple[dict | None, object]:
    """Returns (payload, receipt), or (None, None) when nothing ingested is on-topic.
    `msg` is the topic phrase the planner/drafter agent resolved — no cleanup here.
    `minutes`: booked study time; the outline sizes the page count to fill it.
    payload: {title, markdown, sections, citations, style_used, generation_id}.
    Built in MULTIPLE model calls (outline + one per section) so depth follows
    the topic's complexity; mock mode derives sections from the extractive page."""
    g = get_graph(user)
    chunks = g.relevant_chunks(msg, k=6)
    if not chunks:
        return None, None
    title = " ".join(msg.split())[:60]
    ids = {c["id"] for c in chunks}
    fallback = _extractive_page(title, chunks)
    ctx = "\n".join(f"[{c['id']}] {c['text'][:800]}" for c in chunks)

    sections = _build_sections(user, title, ctx, critiques, fallback, minutes)
    # before the citation check: grouped citations "[a, b]" must count as citing
    sections = [{**s, "markdown": repair_math(normalize_citations(s["markdown"], ids))}
                for s in sections]
    combined = f"# {title}\n\n" + "\n\n".join(s["markdown"] for s in sections)
    cited_ok = any(f"[{cid}]" in combined for cid in ids)
    from ..schemas import Receipt
    from .llm import model_id
    if cited_ok:
        out = {"markdown": combined}
        receipt = Receipt(model=model_id("fast"), verified=True,
                          reason=f"{len(sections)} sections, multi-call")
    else:            # nothing cited: ship the grounded extractive page instead
        out = fallback
        sections = _split_sections(fallback["markdown"])
        receipt = Receipt(model=model_id("fast"), verified=True,
                          reason="extractive fallback (model page: no citations)")
    md = out["markdown"]
    cited = [cid for cid in ids if f"[{cid}]" in md] or list(ids)[:4]
    gen_id = record_generation(user, "page", md[:4000], "", cited)
    by_id = {c["id"]: c for c in chunks}
    cited.sort(key=md.find)                  # numbered in reading order: [1] comes first
    sources = {cid: g.source_of(cid) for cid in cited}
    payload = {"title": title, "markdown": md, "sections": sections,
               "citations": [{"chunk_id": cid, "url": sources[cid].get("url", ""),
                              "title": sources[cid].get("title")
                              or (by_id[cid]["text"][:60] if cid in by_id else "")}
                             for cid in cited],
               "style_used": styles_for(user, title), "generation_id": gen_id}
    set_progress(user, title, gen_id, 0, len(sections))   # the session tracker starts here
    return payload, receipt


def review_and_revise(user: str, request: str, topic: str,
                      payload: dict | None, receipt,
                      minutes: int = 0) -> tuple[dict | None, object, str]:
    """Critic pass over a drafted page (2 passes MAX, mirroring the agent loop):
    the critic judges alignment, grounding and the learner's taste; on 'major' the
    drafter gets ONE revision with the critiques. Returns (payload, receipt, note)."""
    if settings.MOCK or payload is None:
        return payload, receipt, ""
    try:
        out, _ = route("draft_review", prompt_loader.get(
            "draft_review", request=request[:500],
            styles=styles_for(user, topic) or "none yet",
            avoids=avoids_for(user, topic) or "none",
            draft=payload["markdown"][:16000]),      # the whole book, not just page 1
            mock_output={"severity": "pass", "critiques": []})
    except Exception:  # noqa: BLE001 — the critic is best-effort, never a blocker
        return payload, receipt, ""
    severity = str(out.get("severity", "pass")).lower()
    critiques = [c for c in (out.get("critiques") or []) if isinstance(c, dict)][:5]
    if severity != "major" or not critiques:
        return payload, receipt, ("" if severity == "pass" else f"critic: {severity}")
    fixes = "\n".join(f"- {c.get('where', '')}: {c.get('issue', '')} -> {c.get('fix', '')}"
                      for c in critiques)
    revised, receipt2 = draft_material(user, topic, critiques=fixes, minutes=minutes)
    if revised is None:
        return payload, receipt, "critic: major issues (revision unavailable)"
    receipt2.reason = ((receipt2.reason + "; ") if getattr(receipt2, "reason", "") else "") \
        + "critic revision (pass 2 of 2)"
    return revised, receipt2, "critic: revised (pass 2 of 2)"
