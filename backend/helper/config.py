"""Settings loader. Environment + settings.

All model calls go through Vertex AI (Google Cloud credits, ADC auth — no API keys).
MOCK auto-enables when GOOGLE_CLOUD_PROJECT is absent."""
from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# google-genai and Google ADK read these from the environment: pin every Gemini call
# (ours and ADK's) to Vertex AI. "global" serves every current Gemini 3.x model.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")


def _mock_from_env() -> bool:
    return os.getenv("MOCK", "") == "1" or not (os.getenv("GOOGLE_CLOUD_PROJECT", "")
                                                or os.getenv("GOOGLE_CLOUD_API_KEY", ""))


@dataclass
class Settings:
    # Three tiers: lite routes (intent/guard/style, temperature 0), fast does most
    # work, deep (Pro) plans and directs reels. Override any id via .env.
    MODEL_LITE: str = os.getenv("MODEL_LITE", "") or "gemini-3.5-flash-lite"
    MODEL_FAST: str = os.getenv("MODEL_FAST", "") or "gemini-3.8-flash"
    MODEL_DEEP: str = os.getenv("MODEL_DEEP", "") or "gemini-3.1-pro-preview"
    MODEL_EMBED: str = os.getenv("MODEL_EMBED", "") or "gemini-embedding-001"
    # Per-call deadline (seconds) for Gemini requests; a stalled call fails fast and
    # is retried once instead of hanging the chat for minutes.
    LLM_TIMEOUT_S: int = int(os.getenv("LLM_TIMEOUT_S", "90"))
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "change-me")
    GOOGLE_CLOUD_PROJECT: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    GOOGLE_CLOUD_LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    # Optional Vertex AI API key (Google Cloud console, billed to the project's
    # credits). Set: every Gemini call uses it. Blank: ADC / the VM's service account.
    # Firestore and Cloud Storage always use ADC — an API key can't reach them.
    GOOGLE_CLOUD_API_KEY: str = os.getenv("GOOGLE_CLOUD_API_KEY", "")
    MAX_STUDY_MIN_PER_DAY: int = int(os.getenv("MAX_STUDY_MIN_PER_DAY", "150"))
    # Reels: Gemini TTS narration (same client; empty MODEL_TTS = captions only) and the
    # cosine floor for "this chunk is about the topic" (real embeddings only)
    MODEL_TTS: str = os.getenv("MODEL_TTS", "gemini-3.8-flash-tts")
    # Tried when MODEL_TTS is rate-limited (429) or retired (404) — a separate model
    # has its own quota pool, so narration survives a spent quota on the first.
    MODEL_TTS_FALLBACK: str = os.getenv("MODEL_TTS_FALLBACK", "gemini-3.8-flash-lite-tts")
    TTS_VOICE: str = os.getenv("TTS_VOICE", "Kore")
    # Offline fallback voice (pyttsx3 -> OS speech engine): zero quota, zero cost.
    # Used when Gemini TTS is unavailable/exhausted; set LOCAL_TTS=0 to disable.
    LOCAL_TTS: bool = os.getenv("LOCAL_TTS", "1") == "1"
    REEL_MIN_SIMILARITY: float = float(os.getenv("REEL_MIN_SIMILARITY", "0.6"))
    # Google OAuth web client (Calendar read + Docs export). Redirect URI registered in
    # Cloud Console must be exactly OAUTH_REDIRECT_BASE + /api/connect/google/callback
    GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    OAUTH_REDIRECT_BASE: str = os.getenv("OAUTH_REDIRECT_BASE", "http://localhost:8000")
    # ---- External service URLs (single source of truth; override via env) -------
    GOOGLE_AUTH_URL: str = os.getenv(
        "GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    GOOGLE_TOKEN_URL: str = os.getenv(
        "GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    GOOGLE_REVOKE_URL: str = os.getenv(
        "GOOGLE_REVOKE_URL", "https://oauth2.googleapis.com/revoke")
    GOOGLE_CAL_LIST_URL: str = os.getenv(
        "GOOGLE_CAL_LIST_URL", "https://www.googleapis.com/calendar/v3/users/me/calendarList")
    # {} = quoted calendar id
    GOOGLE_EVENTS_URL: str = os.getenv(
        "GOOGLE_EVENTS_URL", "https://www.googleapis.com/calendar/v3/calendars/{}/events")
    GOOGLE_DOCS_URL: str = os.getenv(
        "GOOGLE_DOCS_URL", "https://docs.googleapis.com/v1/documents")
    # {} = document id
    GOOGLE_DOC_EDIT_URL: str = os.getenv(
        "GOOGLE_DOC_EDIT_URL", "https://docs.google.com/document/d/{}/edit")
    GOOGLE_SCOPE_CALENDAR: str = os.getenv(
        "GOOGLE_SCOPE_CALENDAR", "https://www.googleapis.com/auth/calendar.readonly")
    GOOGLE_SCOPE_GDOCS: str = os.getenv(
        "GOOGLE_SCOPE_GDOCS", "https://www.googleapis.com/auth/drive.file")
    # Wikipedia's API policy requires a descriptive User-Agent with contact info
    HTTP_USER_AGENT: str = os.getenv(
        "HTTP_USER_AGENT",
        "Mozilla/5.0 (compatible; RecalBot/0.2; +https://github.com/Chaitanya674/Recal-AI)")
    WIKI_API_URL: str = os.getenv("WIKI_API_URL", "https://en.wikipedia.org/w/api.php")
    WIKI_ARTICLE_BASE: str = os.getenv("WIKI_ARTICLE_BASE", "https://en.wikipedia.org/wiki/")
    # Keyless web search (HTML endpoint) used by find_sources to reach real docs/blogs;
    # Wikipedia is only the fallback when this fails.
    WEB_SEARCH_URL: str = os.getenv("WEB_SEARCH_URL", "https://html.duckduckgo.com/html/")
    # Firestore persistence turns on when GOOGLE_CLOUD_PROJECT is set (docs/firestore-design.md).
    # Media bucket + KMS key for envelope-encrypting stored secrets are optional extras.
    RECAL_MEDIA_BUCKET: str = os.getenv("RECAL_MEDIA_BUCKET", "")
    RECAL_KMS_KEY: str = os.getenv("RECAL_KMS_KEY", "")      # projects/…/cryptoKeys/recal-secrets
    # AES-256-GCM key for secrets when KMS isn't available (32 bytes, base64). Losing it
    # means stored OAuth tokens can't be read — users just reconnect Google.
    RECAL_ENCRYPTION_KEY: str = os.getenv("RECAL_ENCRYPTION_KEY", "")
    MOCK: bool = _mock_from_env()


settings = Settings()
