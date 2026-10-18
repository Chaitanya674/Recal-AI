"""Config + feature-readiness report behind GET /api/health.

Feature status: real (live Gemini / real integration) · mock (works, canned output)
· stub (not built yet) · setup (needs config from you) · broken (config is wrong)."""
from __future__ import annotations

import importlib.util
import os

from ..agents.agent_factory import AgentFactory
from ..database import fire
from ..features import google, knowledge, llm
from .config import settings


def _f(key: str, name: str, status: str, note: str = "") -> dict:
    return {"key": key, "name": name, "status": status, "note": note}


def _google(g: dict, provider: str, connected_note: str) -> tuple[str, str]:
    if g[provider]["connected"]:
        return "real", f"connected as {g[provider]['email'] or 'your Google account'}; {connected_note}"
    if not g["configured"]:
        return "setup", "set GOOGLE_OAUTH_CLIENT_ID/SECRET in .env (TESTING.md)"
    return "setup", "OAuth configured: click the pill in the UI to sign in"


def report(user: str, live: bool = False) -> dict:
    client = llm.get_client()
    problems: list[str] = []
    if settings.MOCK:
        why = ("MOCK=1 in .env" if os.getenv("MOCK", "") == "1"
               else "GOOGLE_CLOUD_PROJECT is not set")
        problems.append(f"Mock mode ({why}): every model call returns canned output.")
    elif client is None:
        problems.append("Real mode but no Vertex AI client: check GOOGLE_CLOUD_PROJECT, "
                        "that the Vertex AI API is enabled, and the credentials (VM service "
                        "account or `gcloud auth application-default login`).")

    llm_ok = llm.available()
    gen = "real" if llm_ok else "mock" if settings.MOCK else "broken"
    adk = AgentFactory().available
    if not settings.MOCK and not adk:
        problems.append("ADK concierge inactive (needs google-adk + Vertex AI): "
                        "free-form chat answers via the chat model directly.")
    firestore = fire.enabled()
    enc = fire.encryption_mode()
    if firestore and enc == "none":
        problems.append("Firestore is on but neither RECAL_ENCRYPTION_KEY nor RECAL_KMS_KEY "
                        "is set: stored secrets (OAuth tokens) are only "
                        "base64-obfuscated, not encrypted.")
    if firestore and not fire.media_enabled():
        problems.append("Firestore is on but RECAL_MEDIA_BUCKET is unset: reel media stays "
                        "on the local disk and is lost when the instance restarts.")
    g = google.status(user)
    render_ok = all(importlib.util.find_spec(m) for m in ("PIL", "imageio_ffmpeg"))
    if not render_ok:
        problems.append("Pillow / imageio-ffmpeg missing: reel video and slide downloads fail.")
    docs_ok = all(importlib.util.find_spec(m) for m in ("pptx", "docx", "openpyxl"))
    if not docs_ok:
        problems.append("python-pptx / python-docx / openpyxl missing: document upload fails.")

    features = [
        _f("ingest", "Ingest a doc (URL)", gen,
           "fetches the page, Gemini extracts concepts, chunks embedded" if llm_ok
           else "no fetch: fake page text, 2 chunks"),
        _f("ask", "Ask a doubt", gen,
           "answers from your graph; Google Search grounding when nothing matches"),
        _f("reel", "Explain visually (reel)", gen,
           "Gemini writes the scene script; a receipt reason of 'extractive fallback' means it "
           "failed validation twice" if llm_ok else "script built from your ingested passages"),
        _f("reel_audio", "Reel narration",
           "mock" if settings.MOCK
           else "real" if (llm_ok and settings.MODEL_TTS) or (settings.LOCAL_TTS and llm.local_tts_available())
           else "setup",
           "captions only in mock mode" if settings.MOCK
           else f"Gemini TTS ({settings.MODEL_TTS}), offline system voice as fallback"
           if llm_ok and settings.MODEL_TTS and settings.LOCAL_TTS and llm.local_tts_available()
           else f"Gemini TTS ({settings.MODEL_TTS})" if llm_ok and settings.MODEL_TTS
           else "offline system voice (pyttsx3) — zero quota"
           if settings.LOCAL_TTS and llm.local_tts_available()
           else "set MODEL_TTS (Gemini) or install pyttsx3 (offline voice)"),
        _f("reel_export", "Reel -> MP4 video / PDF slides", "real" if render_ok else "broken",
           "Pillow + bundled ffmpeg, rendered locally" if render_ok
           else "pip install -r requirements.txt (pillow, imageio-ffmpeg)"),
        _f("plan", "Plan study hours", gen, "places sessions into real free slots"),
        _f("replan", "Urgent meeting / I'm tired", gen, "moves sessions, slot-ID gated"),
        _f("notes", "Write me notes", gen,
           "grounded study page; model-written with citations" if llm_ok
           else "extractive page built from your ingested passages"),
        _f("concierge", "Free-form chat (ADK)",
           "real" if llm_ok and adk else "mock" if settings.MOCK else "broken",
           "ADK concierge with knowledge + calendar tools" if adk
           else "no ADK without Vertex AI: direct chat-model answers"),
        _f("feedback", "Thumbs up/down style memory", "real",
           "stored in memory; learned descriptor text is canned (P5)"),
        _f("gcal", "Google Calendar connect", *_google(g, "calendar",
           "your events are mirrored into the Week view (read-only)")),
        _f("gdocs", "Google Docs connect", *_google(g, "gdocs", "Export creates a new Doc")),
        _f("export", "Export card to Google Docs", "real" if g["gdocs"]["connected"] else "setup",
           "click Export on a card" if g["gdocs"]["connected"] else "connect Google Docs first"),
        _f("docs_upload", "Upload PPTX / DOCX / XLSX", "real" if docs_ok else "broken",
           "parsed into sections + fed to the knowledge graph" if docs_ok
           else "pip install python-pptx python-docx openpyxl"),
        _f("doc_review", "Deck / doc review (message + client-ready)",
           ("real" if llm_ok else "mock") if docs_ok else "broken",
           "heuristic checks always run; Gemini deepens them in real mode" if docs_ok
           else "needs the document libraries"),
        _f("rehearse", "Presentation rehearsal coach",
           ("real" if llm_ok else "mock") if docs_ok else "broken",
           "mic audio understood by Gemini; cross-questions per section" if llm_ok
           else "canned client questions from the section text (audio needs Gemini)"),
        _f("media_store", "Reel media in Cloud Storage",
           "real" if fire.media_enabled() else "setup",
           "signed-URL downloads, survives restarts" if fire.media_enabled()
           else "set RECAL_MEDIA_BUCKET (media is on local disk)"),
        _f("secrets", "Secrets encrypted at rest",
           "real" if enc != "none" else "setup",
           {"kms": "OAuth tokens sealed via Cloud KMS",
            "aes-gcm": "OAuth tokens sealed with AES-256-GCM (RECAL_ENCRYPTION_KEY)",
            "none": "set RECAL_ENCRYPTION_KEY (or RECAL_KMS_KEY) — see docs/firestore-design.md"}[enc]),
        _f("persistence", "Firestore persistence", "real" if firestore else "setup",
           "chat, cards, graph, styles, docs and tokens survive restarts" if firestore
           else "set GOOGLE_CLOUD_PROJECT: until then chat, cards, graph and uploads reset on restart"),
    ]

    checks = None
    if live:
        if settings.MOCK:
            checks = {"skipped": "mock mode: no real calls to make"}
        else:
            checks = {name: {"ok": ok, "detail": detail} for name, (ok, detail) in (
                ("model_lite", llm.ping("lite")), ("model_fast", llm.ping("fast")),
                ("model_deep", llm.ping("deep")),
                ("embeddings", knowledge.ping_embeddings()), ("tts", llm.ping_tts()),
                ("web_search", knowledge.ping_web()))}

    return {"ok": True, "mode": "mock" if settings.MOCK else "real",
            "config": {"llm_backend": "vertex", "client_ready": client is not None,
                       "model_lite": settings.MODEL_LITE, "model_fast": settings.MODEL_FAST,
                       "model_deep": settings.MODEL_DEEP, "adk": adk, "firestore": firestore,
                       "vertex_project": settings.GOOGLE_CLOUD_PROJECT,
                       "vertex_location": settings.GOOGLE_CLOUD_LOCATION,
                       "google_oauth": g["configured"], "oauth_redirect_uri": g["redirect_uri"]},
            "problems": problems, "features": features, "live": checks}
