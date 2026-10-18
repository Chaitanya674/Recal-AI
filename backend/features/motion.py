"""Concept Reels: grounded 60-second scene scripts -> narration -> playable reel package.

make_reel():
  1. relevant chunks only (knowledge.relevant_chunks) — nothing on-topic => no reel
  2. focus_facts (fast tier): keeps ONLY material serving the user's actual request —
     history, inventors, dates and citations are dropped unless asked for
  3. visual_storyboard (deep tier — the Pro model): directs a visual-first story,
     beat by beat; then the fast tier (Flash) compiles it into the SceneScript JSON.
     validate_script checks schema, slots per archetype, <=25-word narration, every
     scene cites a real chunk, <=60 s; one deep-tier retry inside route()
  4. still invalid => extractive fallback built from the chunks, so a reel always plays
     (mock mode uses the same builder, so mock reels follow the topic too)
  5. Gemini TTS per scene (optional) -> scene durations follow the audio
  6. package saved to media/<generation_id>/ for the player and render.py (MP4 / PDF)
The LLMs only fill slots; they never write animation code."""
from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from . import render
from ..database import fire
from .knowledge import get_graph
from . import llm
from .llm import TTS_RATE, _status_of, route, speak
from .memory import record_generation, styles_for
from ..schemas import SceneScript
from ..schemas.reel import (ICON_NAMES, TitleSlots, DiagramSlots, CompareSlots,
                           TimelineSlots, CodeSlots, CounterSlots, CalloutSlots,
                           FlowSlots, BarChartSlots, TokenAttentionSlots,
                           MatrixHeatSlots, VectorSpaceSlots, CycleSlots,
                           KeywordMatchSlots)

_SLOT_MODELS = {"title": TitleSlots, "build_diagram": DiagramSlots,
                "compare_two": CompareSlots, "timeline": TimelineSlots,
                "code_reveal": CodeSlots, "counter": CounterSlots,
                "callout": CalloutSlots, "flow": FlowSlots,
                "bar_chart": BarChartSlots, "token_attention": TokenAttentionSlots,
                "matrix_heat": MatrixHeatSlots, "vector_space": VectorSpaceSlots,
                "cycle": CycleSlots, "keyword_match": KeywordMatchSlots}
_CARRY_KINDS = {"text", "vector", "list", "doc", "packet"}
WPS = 2.5           # narration words/second (no audio)
SCENE_PAD_S = 2.0
AUDIO_PAD_S = 0.8   # breath after each narrated scene


def validate_script(script: dict, graph) -> tuple[bool, str]:
    try:
        s = SceneScript.model_validate(script)
    except ValidationError as e:
        return False, f"schema: {e.errors()[0].get('msg', 'invalid')}"
    dur = 0.0
    for sc in s.scenes:
        try:
            _SLOT_MODELS[sc.archetype].model_validate(sc.slots)
        except ValidationError:
            return False, f"bad slots for archetype {sc.archetype}"
        if len(sc.narration.split()) > 25:
            return False, "narration over 25 words"
        if not graph.has_chunk(sc.grounded_in):
            return False, f"grounded_in {sc.grounded_in!r} is not a chunk"
        dur += len(sc.narration.split()) / WPS + SCENE_PAD_S
    if dur > 60:
        return False, f"estimated duration {dur:.0f}s > 60s"
    return True, ""


# ---- text helpers ----------------------------------------------------------------
def _clip(text: str, n: int) -> str:
    words = str(text).split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(text).strip()) if s.strip()]


def _estimate_s(scenes: list) -> float:
    return sum(len(str(s.get("narration", "")).split()) / WPS + SCENE_PAD_S
               for s in scenes if isinstance(s, dict))


def _repair_carry(c) -> dict | None:
    if isinstance(c, str):
        c = {"kind": "text", "text": c}
    if not isinstance(c, dict) or str(c.get("kind", "")).lower() not in _CARRY_KINDS:
        return None
    return {"kind": str(c["kind"]).lower(), "text": _clip(c.get("text", ""), 6),
            "items": [_clip(x, 5) for x in (c.get("items") or []) if str(x).strip()][:3]}


def _repair_slots(s: dict):
    """Drop what the renderer can't draw instead of rejecting the whole scene:
    unknown icons, malformed carries, edges to missing nodes, a query that isn't
    one of the vectors, a score that isn't a number."""
    arch, sl = s.get("archetype"), s.get("slots")
    if not isinstance(sl, dict):
        return
    if arch == "flow":
        nodes = [n for n in (sl.get("nodes") or []) if isinstance(n, dict)]
        for n in nodes:
            icon = str(n.get("icon", "")).strip().lower()
            if icon in ICON_NAMES:
                n["icon"] = icon
            else:
                n.pop("icon", None)
        ids = {str(n.get("id")) for n in nodes}
        edges = []
        for e in sl.get("edges") or []:
            if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids \
                    or e.get("from") == e.get("to"):
                continue
            carry = _repair_carry(e.get("carry"))
            if carry:
                e["carry"] = carry
            else:
                e.pop("carry", None)
            edges.append(e)
        sl["nodes"], sl["edges"] = nodes, edges
    elif arch == "vector_space":
        labels = {str(v.get("label")) for v in (sl.get("vectors") or []) if isinstance(v, dict)}
        if sl.get("query") not in labels:
            sl.pop("query", None)
        sl["neighbors"] = [x for x in (sl.get("neighbors") or [])
                           if x in labels and x != sl.get("query")]
    elif arch == "keyword_match":
        sl["query"] = " ".join(str(sl.get("query", "")).split()[:4])
        sl["document"] = " ".join(str(sl.get("document", "")).split()[:10])
        score = sl.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            sl.pop("score", None)


def _normalize(out, topic: str) -> dict:
    """Repair the usual LLM drift before validating (renamed keys, [id] brackets...).
    Also fits the script to the hard limits instead of rejecting it outright: a
    slightly-too-long Pro script trimmed to 60s beats the extractive fallback."""
    if not isinstance(out, dict):
        return {}
    out = json.loads(json.dumps(out))
    if "scenes" not in out and isinstance(out.get("script"), dict):
        out = out["script"]
    out.setdefault("title", topic)
    out["aspect"] = "16:9"
    scenes = out.get("scenes") if isinstance(out.get("scenes"), list) else []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        if "archetype" not in s and "type" in s:
            s["archetype"] = s.pop("type")
        if "slots" not in s:
            alt = next((k for k in ("content", "data", "params") if isinstance(s.get(k), dict)), None)
            if alt:
                s["slots"] = s.pop(alt)
        if isinstance(s.get("grounded_in"), str):
            s["grounded_in"] = s["grounded_in"].strip("[] ")
        if isinstance(s.get("narration"), str) and len(s["narration"].split()) > 25:
            s["narration"] = _clip(s["narration"], 25)
        _repair_slots(s)
    if scenes:
        del scenes[8:]                               # schema cap, trimmed not rejected
        while len(scenes) > 3 and _estimate_s(scenes) > 60:
            scenes.pop(len(scenes) - 2)              # keep the hook and the takeaway
    return out


def _fact_sentences(chunks: list[dict], want: int) -> list[tuple[str, str]]:
    """Clean, complete, display-friendly sentences: start uppercase, 8-18 words,
    no citation brackets. Falls back to any 4+-word sentence when sources are thin
    (seed/mock text), so a reel always has something to say."""
    for strict in (True, False):
        out, seen = [], set()
        for ch in chunks:
            for sent in _sentences(ch["text"]):
                words = sent.split()
                if strict and not (8 <= len(words) <= 18 and sent[0].isupper()
                                   and "[" not in sent and "refer to" not in sent.lower()):
                    continue
                if not strict and len(words) < 4:
                    continue
                if sent.lower() in seen:
                    continue
                seen.add(sent.lower())
                out.append((sent, ch["id"]))
                break                      # one per chunk keeps coverage broad
            if len(out) >= want:
                return out
        if out:
            return out
    return []


def _good_labels(labels: list[str]) -> list[str]:
    """Concept labels fit for flow nodes: short, deduped, capitalized — raw triple
    labels ('predecessors of transformers', 'each token') make junk diagrams."""
    out: list[str] = []
    for lb in labels:
        lb = str(lb).strip(" .,:;")
        if 2 < len(lb) <= 22 and len(lb.split()) <= 3 \
                and lb.lower() not in {x.lower() for x in out}:
            out.append(lb[:1].upper() + lb[1:])
    return out[:5]


def _vs_sides(topic: str) -> list[str]:
    sides = [s.strip(" .?!") for s in re.split(r"\s+(?:vs\.?|versus)\s+", topic, flags=re.I)]
    return sides if len(sides) == 2 and all(sides) else []


def _compare_scene(topic: str, facts: list[tuple[str, str]]) -> tuple[dict | None, set[str]]:
    """'X vs Y' deserves two panels facing off, not a run of quote cards. Returns
    (scene or None, the fact sentences it consumed)."""
    sides = _vs_sides(topic)
    if not sides:
        return None, set()
    cols, used = [], set()
    for side in sides:
        key = side.lower().rstrip("s")
        points = [(s, c) for s, c in facts if key in s.lower()]
        if not points:
            return None, set()
        cols.append({"title": side[:1].upper() + side[1:24],
                     "points": [_clip(s, 8) for s, _ in points[:2]]})
        used |= {s for s, _ in points[:2]}
    scene = {"archetype": "compare_two",
             "slots": {"left": cols[0], "right": cols[1]},
             "narration": _clip(f"{sides[0]} and {sides[1]}, side by side.", 22),
             "grounded_in": next(c for s, c in facts if s in used)}
    return scene, used


def extractive_script(topic: str, chunks: list[dict], labels: list[str]) -> dict:
    """A valid, grounded script built straight from the source text (no model).
    Shape: title hook -> animated flow of the key concepts -> a compare panel for
    "X vs Y" topics -> one clean fact per scene -> takeaway. This is what ships
    when the script model is unavailable, so it has to read like an explainer,
    not a wall of raw sentences."""
    facts = _fact_sentences(chunks, want=5)
    anchor = chunks[0]["id"]
    scenes = [{"archetype": "title",
               "slots": {"title": topic,
                         "subtitle": f"grounded in {len(chunks)} captured passage{'s' if len(chunks) != 1 else ''}"},
               "narration": _clip(f"Here is {topic} in under a minute, from your own sources.", 20),
               "grounded_in": anchor}]
    good = _good_labels(labels)
    if len(good) >= 3:                     # the mechanism up front, not as an afterthought
        nodes = [{"id": f"n{i}", "label": lb} for i, lb in enumerate(good)]
        edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]}
                 for i in range(len(nodes) - 1)]
        scenes.append({"archetype": "flow",
                       "slots": {"nodes": nodes, "edges": edges},
                       "narration": _clip("How the pieces connect: " + ", ".join(good) + ".", 22),
                       "grounded_in": anchor})
    compare, used = _compare_scene(topic, facts)
    callouts = facts
    if compare:
        scenes.append(compare)
        callouts = [(s, c) for s, c in facts if s not in used][:2]
    for sent, cid in callouts:
        scenes.append({"archetype": "callout", "slots": {"text": _clip(sent, 14)},
                       "narration": _clip(sent, 22), "grounded_in": cid})
    if facts:                              # close on a takeaway, not a trailing fact
        scenes.append({"archetype": "callout",
                       "slots": {"text": _clip(f"Takeaway: {facts[0][0]}", 14)},
                       "narration": _clip(f"If you remember one thing: {facts[0][0]}", 22),
                       "grounded_in": facts[0][1]})
    return {"title": topic, "aspect": "16:9", "scenes": scenes[:8]}


# ---- intent focus + visual direction (the LLM stages before scene JSON) -------------
_FORMS = {"title", "flow", "bar_chart", "compare_two", "timeline", "counter",
          "build_diagram", "code_reveal", "callout", "token_attention",
          "matrix_heat", "vector_space", "cycle", "keyword_match"}


def _facts_block(facts: list[dict]) -> str:
    return "\n".join(f"[{f['id']}] {f['fact']}" for f in facts)


def _clean_facts(out, chunk_ids: set[str]) -> list[dict]:
    facts = []
    for f in (out.get("facts") if isinstance(out, dict) else None) or []:
        cid = str(f.get("id", "")).strip("[] ") if isinstance(f, dict) else ""
        if cid in chunk_ids and f.get("fact"):
            facts.append({"id": cid, "fact": _clip(str(f["fact"]), 28)})
    return facts[:12]


def focus_facts(request: str, topic: str, chunks: list[dict]) -> tuple[list[dict], str]:
    """LLM relevance filter: keep only material serving the user's actual request —
    mechanisms, comparisons, numbers — and drop history/inventors/citations unless
    asked. Returns ([], "") when no model is reachable or the filter fails; the
    pipeline then falls back to the raw chunks, never to no reel."""
    if not llm.available():
        return [], ""
    ids = {c["id"] for c in chunks}
    ctx = "\n".join(f"[{c['id']}] {c['text'][:800]}" for c in chunks)
    from ..helper import prompt_loader
    prompt = prompt_loader.get("reel_focus", request=request, topic=topic, chunks=ctx)
    out, _ = route("reel_focus", prompt, domain=topic[:24], mock_output={},
                   verifier=lambda o: (len(_clean_facts(o, ids)) >= 3,
                                       "fewer than 3 facts grounded in real passage ids"))
    facts = _clean_facts(out, ids)
    angle = " ".join(str(out.get("angle", "")).split()) if isinstance(out, dict) else ""
    return (facts, angle) if len(facts) >= 3 else ([], "")


def _clean_beats(out, fact_ids: set[str]) -> list[dict]:
    beats = []
    for b in (out.get("storyboard") if isinstance(out, dict) else None) or []:
        if not isinstance(b, dict):
            continue
        gid = str(b.get("grounded_in", "")).strip("[] ")
        if gid not in fact_ids or not b.get("narration"):
            continue
        form = str(b.get("form", "")).strip().lower()
        beats.append({"visual": _clip(str(b.get("visual", "")), 40),
                      "form": form if form in _FORMS else "flow",
                      "labels": [_clip(str(lb), 4) for lb in (b.get("labels") or [])[:6]],
                      "narration": _clip(str(b["narration"]), 15),
                      "grounded_in": gid})
    return beats[:7]


def visual_storyboard(request: str, topic: str, facts: list[dict],
                      angle: str, styles: str) -> list[dict]:
    """The Pro (deep-tier) model directs the reel: a visual-first storyboard —
    what moves, builds or compares on screen, beat by beat. [] when unavailable."""
    if not llm.available() or not facts:
        return []
    ids = {f["id"] for f in facts}
    from ..helper import prompt_loader
    prompt = prompt_loader.get("reel_visual_script", request=request,
                               angle=angle or f"answer the request: {request}",
                               facts=_facts_block(facts), styles=styles)
    out, _ = route("reel_visual_script", prompt, domain=topic[:24], mock_output={},
                   verifier=lambda o: (4 <= len(_clean_beats(o, ids)) <= 7,
                                       "need 4-7 beats, each grounded in a fact id"))
    beats = _clean_beats(out, ids)
    return beats if len(beats) >= 4 else []


# ---- narration ---------------------------------------------------------------------
def narrate(user: str, gen_id: str, script: dict) -> tuple[list[float], list[str], str]:
    """Gemini TTS per scene — SEQUENTIAL: a parallel burst trips the free tier's tiny
    per-minute TTS limit and used to silence the whole reel. A 429 waits once and
    retries; a scene that still fails gets silence of the estimated length, so one
    rate-limited scene no longer mutes the others. Returns (durations, names, note)."""
    scenes = script["scenes"]
    estimate = [round(len(s["narration"].split()) / WPS + SCENE_PAD_S, 2) for s in scenes]
    clips: list[bytes | None] = []
    engines: set[str] = set()
    waits = 0
    first_err = ""
    for s in scenes:
        clip = None
        for attempt in (0, 1):
            try:
                clip = speak(s["narration"])
                engines.add(llm.last_tts_engine)
                break
            except Exception as e:  # noqa: BLE001 — narration is optional
                first_err = first_err or f"{type(e).__name__}: {str(e)[:80]}"
                if _status_of(e) == 429 and attempt == 0 and waits < 2:
                    waits += 1
                    time.sleep(15)        # one pause usually clears a per-minute cap
                    continue
                break
        clips.append(clip)
    if not any(clips):
        return estimate, [], f"captions only ({first_err or 'narration unavailable'})"
    d = render.package_dir(gen_id)
    d.mkdir(parents=True, exist_ok=True)
    names, durations, ok = [], [], 0
    for i, pcm in enumerate(clips):
        names.append(f"scene{i}.wav")
        if pcm is None:                   # rate-limited scene: silence, correct length
            pcm = b"\0\0" * int(estimate[i] * TTS_RATE)
        else:
            ok += 1
        render.write_wav(d / names[-1], pcm)
        durations.append(round(max(len(pcm) / (2 * TTS_RATE) + AUDIO_PAD_S, 2.5), 2))
        if fire.media_enabled():
            try:
                fire.blob_upload(f"users/{user}/reels/{gen_id}/{names[-1]}",
                                 (d / names[-1]).read_bytes(), "audio/wav")
            except Exception:  # noqa: BLE001
                pass
    base = "+".join(sorted(engines)) if engines else "gemini-tts"
    note = (base if ok == len(clips)
            else f"{base} ({ok}/{len(clips)} scenes narrated; rest rate-limited)")
    return durations, names, note


# ---- pipeline ------------------------------------------------------------------------
def make_reel(user: str, topic: str, request: str = "") -> tuple[dict | None, object]:
    """Returns (None, None) when nothing ingested is about the topic.
    `topic` is the phrase the planner/drafter agent resolved — no cleanup here.
    `request` is the learner's original ask, verbatim: the focus stage uses it to
    keep only what the learner cares about (no history/trivia unless asked)."""
    g = get_graph(user)
    chunks = g.relevant_chunks(topic, k=8)    # 6-8 scenes need richer source material
    if not chunks:
        return None, None
    title = " ".join(topic.split())[:60]
    request = " ".join((request or "").split())[:300] or f"Explain {title} visually"
    ids = {c["id"] for c in chunks}
    labels = list(dict.fromkeys(g.nodes[e["to"]]["label"] for e in g.edges
                                if e["rel"] == "MENTIONS" and e["from"] in ids and e["to"] in g.nodes))
    fallback = extractive_script(title, chunks, labels)
    styles = styles_for(user, title) or "none yet"
    from ..helper import prompt_loader
    # Stage 1 (fast): keep only facts serving the request. Stage 2 (deep/Pro): direct
    # the visuals. Stage 3 (fast/Flash): compile the storyboard into scene JSON.
    # Every stage degrades: no facts -> raw chunks; no storyboard -> one-shot script.
    facts, angle = focus_facts(request, title, chunks)
    beats = visual_storyboard(request, title, facts, angle, styles)
    if beats:
        kind = "reel_scene_json"
        prompt = prompt_loader.get(kind, topic=title, facts=_facts_block(facts),
                                   storyboard=json.dumps({"title": title, "storyboard": beats}))
    else:
        kind = "reel_script"
        ctx = _facts_block(facts) if facts else "\n".join(f"[{c['id']}] {c['text'][:800]}"
                                                          for c in chunks)
        prompt = prompt_loader.get(kind, topic=title, chunks=ctx, styles=styles)
    out, receipt = route(kind, prompt,
                         verifier=lambda o: validate_script(_normalize(o, title), g),
                         mock_output=fallback, domain=title[:24])
    crashed = out is fallback and llm.available()    # route() returned our fallback object:
    out = _normalize(out, title)                     # both model attempts crashed outright
    ok, why = validate_script(out, g)
    if not ok:     # model failed twice: never ship an unplayable reel
        out = fallback
        receipt.verified, receipt.reason = True, f"extractive fallback (model script: {why})"
    elif crashed:  # fallback shipped because the model was unreachable — say which error
        receipt.verified = True
        receipt.reason = f"extractive fallback (script model unreachable: {receipt.reason})"[:300]
    gen_id = record_generation(user, "reel", json.dumps(out)[:4000], "",
                               [s.get("grounded_in", "") for s in out.get("scenes", [])])
    durations, audio_files, narration = narrate(user, gen_id, out)
    render.save_package(gen_id, {"user": user, "title": out.get("title", title), "scene_script": out,
                                 "durations": durations, "audio_files": audio_files})
    render.prerender(gen_id)      # MP4 ready by the time someone clicks Download
    audio = [f"/api/reels/{gen_id}/files/{n}" for n in audio_files] or None
    return {"scene_script": out, "durations": durations, "audio": audio,
            "narration": narration, "generation_id": gen_id}, receipt
