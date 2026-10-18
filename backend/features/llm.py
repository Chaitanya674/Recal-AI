"""Gemini client (Vertex AI) + Switchboard routing.

Rules implemented here (see RECAL_BUILD_PLAN_GCP.md §1):
- One backend: Vertex AI via google-genai, authenticated with ADC (the VM's service
  account in production, `gcloud auth application-default login` locally).
- MOCK mode: no SDK import needed, canned streams/JSON.
- route(): tier + thinking level by task kind; verifier -> one deep retry with
  the failure reason appended; every routed call is logged.
- extract_json(): whole parse -> last ```json fence -> last balanced {...}.
"""
from __future__ import annotations

import io
import json
import re
import time
import wave
from typing import Callable, Iterator

from ..helper.config import settings
from ..helper.logger import get_logger
from ..schemas import Receipt

# google-genai is only needed in real mode; keep the app importable without it.
try:
    from google import genai  # type: ignore
    from google.genai import types as genai_types  # type: ignore

    # Server-side deadline per call: a request stalled in a congested queue comes back
    # as 504 DEADLINE_EXCEEDED (retried once below) instead of hanging for minutes.
    _http = genai_types.HttpOptions(timeout=settings.LLM_TIMEOUT_S * 1000)
    if settings.MOCK:
        _client = None
    elif settings.GOOGLE_CLOUD_API_KEY:      # API key: served from the global endpoint
        _client = genai.Client(vertexai=True, api_key=settings.GOOGLE_CLOUD_API_KEY,
                               http_options=_http)
    elif settings.GOOGLE_CLOUD_PROJECT:
        _client = genai.Client(vertexai=True, project=settings.GOOGLE_CLOUD_PROJECT,
                               location=settings.GOOGLE_CLOUD_LOCATION, http_options=_http)
    else:
        _client = None
except Exception:  # noqa: BLE001
    _client = None


def get_client():
    """Shared Vertex AI client for other modules, e.g. knowledge.embed."""
    return _client

_MODELS = {"lite": settings.MODEL_LITE, "fast": settings.MODEL_FAST,
           "deep": settings.MODEL_DEEP}

# ---------------------------------------------------------------------------
# transient-error retry: under load Vertex answers 503 UNAVAILABLE ("model is
# experiencing high demand") or 429 RESOURCE_EXHAUSTED (shared capacity momentarily
# full, not a hard quota) — retry with growing delay before giving up. A 504 (our
# LLM_TIMEOUT_S deadline expired) is retried once only: each one costs a full deadline.
# ---------------------------------------------------------------------------
RETRIES, RETRY_DELAY_S = 3, 2.0
_TRANSIENT = (429, 503)


def _retryable(e: Exception, attempt: int) -> bool:
    status = _status_of(e)
    return status in _TRANSIENT or (status == 504 and attempt == 0)


def _status_of(e: Exception) -> int:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if not code:
        code = getattr(getattr(e, "response", None), "status_code", None)
    try:
        return int(code or 0)
    except (TypeError, ValueError):
        return 0


def with_retry(call):
    """Run call(); on a 429/503 retry up to RETRIES times (2s, 4s, 6s), on a 504
    once. Any other error raises immediately — only overload is worth waiting out."""
    for attempt in range(RETRIES + 1):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 — re-raised unless transient
            if not _retryable(e, attempt) or attempt == RETRIES:
                raise
            time.sleep(RETRY_DELAY_S * (attempt + 1))


def _stream_with_retry(make_iter):
    """Same policy for streams, but only before the first chunk was yielded —
    retrying mid-stream would duplicate text the user already saw."""
    for attempt in range(RETRIES + 1):
        yielded = False
        try:
            for chunk in make_iter():
                yielded = True
                yield chunk
            return
        except Exception as e:  # noqa: BLE001 — re-raised unless transient
            if yielded or not _retryable(e, attempt) or attempt == RETRIES:
                raise
            time.sleep(RETRY_DELAY_S * (attempt + 1))


def _no_llm() -> bool:
    """True when every model call must take the canned/mock path."""
    return settings.MOCK or _client is None


def available() -> bool:
    """A real model backend is reachable (callers can skip optional LLM passes)."""
    return not _no_llm()


def model_id(tier: str) -> str:
    """Active model id, for receipts."""
    return f"mock-{tier}" if settings.MOCK else _MODELS[tier]


def llm_status() -> dict:
    """Read-only model configuration for the UI (models are set in .env only)."""
    return {"mock": settings.MOCK, "source": "none" if _no_llm() else "vertex",
            "auth": "api_key" if settings.GOOGLE_CLOUD_API_KEY else "service_account",
            "project": settings.GOOGLE_CLOUD_PROJECT,
            "location": settings.GOOGLE_CLOUD_LOCATION, **{
                f"model_{t}": m for t, m in _MODELS.items()}}


# Gemini 3.x thinks in levels, not token budgets. Flash and Pro reject MINIMAL, so
# "none" only goes all the way down on the lite tier.
_THINKING_LEVEL = {"none": "MINIMAL", "low": "LOW", "high": "HIGH"}


def _thinking(tier: str, thinking: str):
    level = _THINKING_LEVEL[thinking]
    if level == "MINIMAL" and tier != "lite":
        level = "LOW"
    return genai_types.ThinkingConfig(thinking_level=level)


def _config(tier: str, thinking: str, **kw):
    """Generation config per tier. Routing (lite) runs at temperature 0 so the same
    message always routes the same way."""
    if tier == "lite":
        kw.setdefault("temperature", 0.0)
    return genai_types.GenerateContentConfig(thinking_config=_thinking(tier, thinking), **kw)


TTS_RATE = 24000      # pipeline-wide PCM format: 16-bit mono at 24 kHz
last_tts_engine = "gemini-tts"   # provenance of the most recent speak() (narrate reads it)


def local_tts_available() -> bool:
    """Offline system voice present? (pyttsx3 -> SAPI on Windows, espeak on Linux)."""
    try:
        import pyttsx3  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _to_pcm(pcm: bytes, rate: int, channels: int, width: int) -> bytes:
    """Resample to the pipeline's 16-bit mono TTS_RATE PCM. (Python >= 3.13 needs
    the audioop-lts package — pinned in requirements.)"""
    import audioop
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != TTS_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TTS_RATE, None)
    return pcm


def _wav_to_pcm(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as w:
        return _to_pcm(w.readframes(w.getnframes()), w.getframerate(),
                       w.getnchannels(), w.getsampwidth())


def _local_speak(text: str) -> bytes:
    """Zero-quota offline narration via the OS speech engine."""
    import os
    import tempfile

    import pyttsx3
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        engine = pyttsx3.init()           # fresh engine per call: reuse hangs on Windows
        engine.setProperty("rate", 175)
        engine.save_to_file(text, path)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:  # noqa: BLE001
            pass
        with open(path, "rb") as f:
            pcm = _wav_to_pcm(f.read())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if len(pcm) < TTS_RATE // 10:         # < 0.05s = synthesis silently failed
        raise RuntimeError("local TTS produced no audio")
    return pcm


def speak(text: str) -> bytes:
    """Narration audio (raw PCM, see TTS_RATE). Priority: Gemini TTS (MODEL_TTS,
    then MODEL_TTS_FALLBACK — separate quota pools) -> offline system voice
    (LOCAL_TTS, zero quota) -> raise. MOCK never voices (tests stay deterministic)."""
    global last_tts_engine
    if settings.MOCK:
        raise RuntimeError("TTS disabled in mock mode")
    last: Exception | None = None
    if _client is not None and settings.MODEL_TTS:
        models = [settings.MODEL_TTS]
        if settings.MODEL_TTS_FALLBACK and settings.MODEL_TTS_FALLBACK not in models:
            models.append(settings.MODEL_TTS_FALLBACK)
        for model in models:
            try:
                resp = with_retry(lambda m=model: _client.models.generate_content(
                    model=m, contents=f"Say in a clear, friendly teaching voice: {text}",
                    config=genai_types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=genai_types.SpeechConfig(voice_config=genai_types.VoiceConfig(
                            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                voice_name=settings.TTS_VOICE))))))
                data = resp.candidates[0].content.parts[0].inline_data.data
                if not data:
                    raise RuntimeError("TTS returned no audio")
                if data[:4] == b"RIFF":     # Gemini 3.x TTS returns a complete WAV file
                    data = _wav_to_pcm(data)
                last_tts_engine = "gemini-tts"
                return data
            except Exception as e:  # noqa: BLE001 — fall through the chain
                last = e
    if settings.LOCAL_TTS and local_tts_available():
        try:
            pcm = _local_speak(text)
            last_tts_engine = "local-tts"
            return pcm
        except Exception as e:  # noqa: BLE001
            last = last or e
    raise last or RuntimeError(
        "TTS disabled (no Gemini client/MODEL_TTS and no local system voice)")


def grounded_search(prompt: str) -> tuple[dict, list[str]]:
    """One fast-tier call with Google Search grounding: (parsed JSON from the reply,
    source URIs from the grounding metadata). Raises without a Gemini client —
    callers fall back to the keyless HTML search."""
    if _no_llm():
        raise RuntimeError("no Gemini client (mock mode)")
    cfg = _config("fast", "low", tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())])
    resp = with_retry(lambda: _client.models.generate_content(
        model=_MODELS["fast"], contents=prompt, config=cfg))
    meta: list[str] = []
    for cand in resp.candidates or []:
        gm = getattr(cand, "grounding_metadata", None)
        for ch in getattr(gm, "grounding_chunks", None) or []:
            uri = getattr(getattr(ch, "web", None), "uri", None)
            if uri and uri not in meta:
                meta.append(uri)
    try:
        data = extract_json(resp.text or "")
    except ValueError:
        data = {}
    return data, meta


def hear_json(prompt: str, audio: bytes | None, mime: str = "audio/wav") -> tuple[dict, Receipt]:
    """One fast-tier JSON call that can also listen to an audio clip (rehearsal coach).
    Raises in mock mode / without a client — callers provide their own fallback."""
    if _no_llm():
        raise RuntimeError("no Gemini client (mock mode)")
    t0 = time.time()
    contents = [prompt]
    if audio:
        contents.append(genai_types.Part.from_bytes(data=audio, mime_type=mime))
    cfg = _config("fast", "low", response_mime_type="application/json")
    resp = with_retry(lambda: _client.models.generate_content(
        model=_MODELS["fast"], contents=contents, config=cfg))
    data = extract_json(resp.text or "")
    receipt = Receipt(model=_MODELS["fast"], thinking="low",
                      ms=int((time.time() - t0) * 1000))
    _log(receipt, "fast")
    return data, receipt


def ping_tts() -> tuple[bool, str]:
    try:
        pcm = speak("Testing narration.")
        engine = (f"{settings.MODEL_TTS} ({settings.TTS_VOICE})" if last_tts_engine == "gemini-tts"
                  else "offline system voice (Gemini TTS failed)")
        return last_tts_engine == "gemini-tts", f"{engine}: {len(pcm) / (2 * TTS_RATE):.1f}s of audio"
    except Exception as e:  # noqa: BLE001
        return False, f"{settings.MODEL_TTS or 'MODEL_TTS empty'}: {type(e).__name__}: {str(e)[:200]}"


def ping(tier: str) -> tuple[bool, str]:
    """One tiny real call (unmetered) — proves credentials + model id for /api/health."""
    if _client is None:
        return False, ("no Vertex AI client: set GOOGLE_CLOUD_PROJECT and give the "
                       "service account roles/aiplatform.user")
    if not _MODELS[tier]:
        return False, f"MODEL_{tier.upper()} is empty"
    try:
        r = with_retry(lambda: _client.models.generate_content(
            model=_MODELS[tier], contents="Reply with the single word OK."))
        return True, f"{_MODELS[tier]} replied {(r.text or '').strip()[:20]!r}"
    except Exception as e:  # noqa: BLE001
        return False, f"{_MODELS[tier]}: {type(e).__name__}: {str(e)[:200]}"


_escalation_counts: dict[tuple[str, str], int] = {}
_logger = get_logger(__name__)


def _log(receipt: Receipt, tier: str):
    _logger.info("llm %s/%s %s%s%s (%d ms)%s", tier, receipt.thinking, receipt.model,
                 "" if receipt.verified else " UNVERIFIED",
                 " escalated" if receipt.escalated else "", receipt.ms,
                 f" — {receipt.reason}" if receipt.reason else "")


# ---------------------------------------------------------------------------
# streaming + json calls
# ---------------------------------------------------------------------------
_MOCK_REPLIES = {
    "plan": "Planned around your real gaps — kept the day before your deadline light.",
    "explain": "Here is the one-minute visual, grounded in your captured sources.",
    "default": "Working from your knowledge graph where possible; ask me to plan it, "
               "make material, or explain it visually.",
}


def chat_stream(prompt: str, tier: str = "fast", thinking: str = "low",
                ground: bool = False) -> Iterator[str]:
    """Yield text deltas. MOCK: canned words with tiny delays."""
    if _no_llm():
        key = next((k for k in _MOCK_REPLIES if k in prompt.lower()), "default")
        for word in _MOCK_REPLIES[key].split(" "):
            time.sleep(0.015)
            yield word + " "
        return
    cfg = _config(tier, thinking, tools=(
        [genai_types.Tool(google_search=genai_types.GoogleSearch())] if ground else None))
    yield from _stream_with_retry(lambda: (
        chunk.text for chunk in _client.models.generate_content_stream(
            model=_MODELS[tier], contents=prompt, config=cfg) if chunk.text))


def chat_json(prompt: str, tier: str = "deep", thinking: str = "high",
              schema=None) -> tuple[dict, Receipt]:
    """Non-streamed structured call. One repair retry on parse failure."""
    t0 = time.time()
    if _no_llm():
        return {}, Receipt(model=f"mock-{tier}", thinking=thinking,
                           ms=int((time.time() - t0) * 1000))
    cfg = _config(tier, thinking, response_mime_type="application/json",
                  response_schema=schema)
    resp = with_retry(lambda: _client.models.generate_content(
        model=_MODELS[tier], contents=prompt, config=cfg))
    try:
        data = extract_json(resp.text or "")
    except ValueError:
        repair = f"{prompt}\n\nYour previous reply was not valid JSON. Reply with ONLY one JSON object."
        resp = with_retry(lambda: _client.models.generate_content(
            model=_MODELS[tier], contents=repair, config=cfg))
        data = extract_json(resp.text or "")
    receipt = Receipt(model=_MODELS[tier], thinking=thinking,
                      ms=int((time.time() - t0) * 1000))
    return data, receipt


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for block in reversed(_FENCE.findall(text)):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    for start in [m.start() for m in re.finditer(r"\{", text)][::-1]:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"No parseable JSON in: {text[:160]}...")


# ---------------------------------------------------------------------------
# Switchboard
# ---------------------------------------------------------------------------
_TIER_BY_KIND = {
    "chat": ("fast", "low"), "ask": ("fast", "low"), "reel_script": ("fast", "low"),
    # reel pipeline: fast pass distills what the USER asked for, the Pro (deep) pass
    # directs the visuals, and a fast pass compiles the storyboard into scene JSON.
    "reel_focus": ("fast", "low"), "reel_visual_script": ("deep", "high"),
    "reel_scene_json": ("fast", "low"), "model_knowledge": ("fast", "low"),
    "page": ("fast", "low"), "intent": ("lite", "none"), "style": ("lite", "none"),
    "plan": ("deep", "high"), "replan": ("deep", "high"), "session_edit": ("deep", "high"),
    "doc_review": ("fast", "low"), "guard": ("lite", "none"),
    "draft_review": ("fast", "high"),      # the critic recomputes worked-example math
}


def route(kind: str, prompt: str,
          verifier: Callable[[dict], tuple[bool, str]] | None = None,
          domain: str = "", schema=None,
          mock_output: dict | None = None) -> tuple[dict, Receipt]:
    """SWITCHBOARD: classify -> execute -> verify -> escalate once with diagnosis.
    NEVER raises: a model crash or unparseable reply counts as failed verification, and after the escalation the
    caller gets its deterministic mock_output with verified=False — every caller
    is built to degrade on that, so a bad model must not 500 an endpoint."""
    tier, thinking = _TIER_BY_KIND.get(kind, ("fast", "low"))
    if _escalation_counts.get((kind, domain), 0) >= 3:
        tier, thinking = "deep", "high"           # learned routing policy

    if _no_llm():
        out = mock_output or {}
        ok, reason = verifier(out) if verifier else (True, "")
        r = Receipt(model=f"mock-{tier}", thinking=thinking, verified=ok, reason=reason)
        _log(r, tier)
        return out, r

    def attempt(t: str, th: str, p: str):
        try:
            o, r = chat_json(p, tier=t, thinking=th, schema=schema)
            return o, r, ""
        except Exception as e:  # noqa: BLE001 — treated as a failed attempt below
            return None, None, f"{type(e).__name__}: {str(e)[:140]}"

    out, receipt, err = attempt(tier, thinking, prompt)
    ok, reason = (verifier(out) if verifier else (True, "")) if not err else (False, err)
    if not ok:
        _escalation_counts[(kind, domain)] = _escalation_counts.get((kind, domain), 0) + 1
        out2, receipt2, err2 = attempt(
            "deep", "high",
            f"{prompt}\n\nYour previous answer failed verification: {reason}. Fix it.")
        if not err2:
            out, receipt = out2, receipt2
            receipt.escalated = True
            ok, reason = verifier(out) if verifier else (True, "")
        else:
            ok, reason = False, err2
    if out is None or receipt is None:            # both attempts crashed: deterministic fallback
        out = mock_output or {}
        receipt = Receipt(model=model_id(tier), thinking=thinking, escalated=True)
    receipt.verified = ok
    receipt.reason = reason
    _log(receipt, tier)
    return out, receipt
