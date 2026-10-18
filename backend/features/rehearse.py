"""Presentation rehearsal: the learner walks an uploaded deck/doc section by section,
speaks the slide into the mic, and Recal plays a gentle client — short feedback on what
they said versus what the slide claims, plus one cross-question a real client might ask.

Real mode: the browser's audio blob is converted to 16 kHz WAV (bundled ffmpeg) and sent
to Gemini with the section text; the model returns transcript + feedback + question.
Mock mode / no mic: a question is built from the section itself (acronyms, bare numbers,
generic headers), so the feature is demoable with zero keys."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from . import documents
from ..database import fire, schema
from .llm import hear_json
from ..helper.config import settings
from ..schemas import Receipt

MAX_AUDIO = 10 * 1024 * 1024
_asked: dict[tuple[str, str], list[str]] = {}    # (user, doc_id) -> questions already asked


def _asked_for(user: str, doc_id: str) -> list[str]:
    key = (user, doc_id)
    if key not in _asked and fire.enabled():
        try:
            doc = fire.get_doc(*schema.rehearse(user), doc_id)
            _asked[key] = list((doc or {}).get("asked", []))
        except Exception:  # noqa: BLE001
            pass
    return _asked.setdefault(key, [])


def _save_asked(user: str, doc_id: str):
    if not fire.enabled():
        return
    try:
        from datetime import datetime, timedelta, timezone
        fire.set_doc({"asked": _asked[(user, doc_id)][-20:],
                      "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
                      "meta": fire.meta()}, *schema.rehearse(user), doc_id)
    except Exception:  # noqa: BLE001
        pass


def _to_wav(audio: bytes) -> bytes:
    """webm/ogg/anything-the-browser-recorded -> 16 kHz mono WAV for Gemini."""
    import imageio_ffmpeg
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.bin", Path(td) / "out.wav"
        src.write_bytes(audio)
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                            "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise documents.DocError("audio_unreadable")
        return dst.read_bytes()


# ---- heuristic coach (mock mode, and the fallback when the model call fails) --------
def _candidate_questions(s: dict) -> list[str]:
    qs = []
    text = documents.section_text(s)
    for a in sorted(set(re.findall(r"\b[A-Z]{2,5}\d?\b", text)))[:3]:
        qs.append(f"You mention “{a}” — assume I don't know the term. What is it, in one sentence?")
    for h in (s.get("headers") or []):
        if h and h.lower() in documents.GENERIC:
            qs.append(f"In this sheet, what exactly does the “{h}” column measure, and in what unit?")
    for ln in s.get("lines", []):
        m = documents._NUMBER.search(ln)
        if m and len(ln.split()) <= 10:
            qs.append(f"Where does the figure {m.group(0)} come from — what's the baseline?")
            break
    qs += [f"If I only remember one thing from “{(s.get('title') or 'this part')[:60]}”, what should it be?",
           "What would you need from us to make this happen?",
           "What's the biggest risk here that we're not seeing on the page?"]
    return qs


def _mock_coach(user: str, doc: dict, s: dict, heard: bool) -> dict:
    asked = _asked_for(user, doc["id"])
    question = next((q for q in _candidate_questions(s) if q not in asked),
                    "Walk me through this part once more, slower.")
    asked.append(question)
    _save_asked(user, doc["id"])
    fb = ("Heard you. (Replay mode: I can't transcribe audio without model keys, "
          "but here's the kind of question a client would ask.)" if heard else
          "Here's the kind of question a client would ask on this part.")
    return {"transcript": "", "feedback": fb, "question": question}


def _valid_coach(out: dict) -> bool:
    return isinstance(out, dict) and bool(out.get("question"))


# ---- the coach turn ------------------------------------------------------------------
def coach(user: str, doc_id: str, section_no: int, audio: bytes | None,
          mime: str = "") -> tuple[dict, Receipt]:
    doc = documents.get_doc(user, doc_id)
    sections = doc["sections"]
    if not 1 <= section_no <= len(sections):
        raise documents.DocError("bad_section")
    s = sections[section_no - 1]
    if audio and len(audio) > MAX_AUDIO:
        raise documents.DocError("audio_too_large")

    if settings.MOCK:
        return _mock_coach(user, doc, s, heard=bool(audio)), Receipt(model="mock-coach")

    asked = _asked_for(user, doc_id)
    from ..helper import prompt_loader
    prompt = prompt_loader.get(
        "rehearse_coach", doc=doc["name"], n=s["n"], total=len(sections),
        section=documents.section_text(s)[:2000],
        asked="\n".join(f"- {q}" for q in asked[-5:]) or "none",
        heard="An audio recording of the presenter follows." if audio
              else "No audio this turn — base your question on the section alone.")
    try:
        out, receipt = hear_json(prompt, _to_wav(audio) if audio else None)
        if not _valid_coach(out):
            raise ValueError("no question in model output")
        out = {"transcript": str(out.get("transcript", ""))[:2000],
               "feedback": str(out.get("feedback", ""))[:600],
               "question": str(out["question"])[:300]}
    except documents.DocError:
        raise
    except Exception:  # noqa: BLE001 — rehearsal must never dead-end mid-practice
        out, receipt = _mock_coach(user, doc, s, heard=bool(audio)), Receipt(
            model="coach-fallback", verified=False, reason="model call failed")
    _asked[(user, doc_id)].append(out["question"])
    _save_asked(user, doc_id)
    return out, receipt


def reset(user: str, doc_id: str):
    _asked.pop((user, doc_id), None)
    if fire.enabled():
        try:
            fire.delete_doc(*schema.rehearse(user), doc_id)
        except Exception:  # noqa: BLE001
            pass
