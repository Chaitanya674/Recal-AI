import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .helper import guardrails, identity
from .helper.config import settings
from .helper.logger import get_logger
from .router.auth_router import router as auth_router
from .router.chat_router import router as chat_router
from .router.doc_router import router as doc_router
from .router.meeting_router import router as meeting_router
from .router.meta_router import router as meta_router
from .router.reel_router import router as reel_router

log = get_logger("recal")

app = FastAPI(title="Recal", version="0.2")
app.include_router(chat_router)
app.include_router(auth_router)
app.include_router(meta_router)
app.include_router(reel_router)
app.include_router(doc_router)
app.include_router(meeting_router)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith(guardrails.RATE_LIMITED_PREFIXES):
        who = identity.current_user(request)      # per visitor, never shared
        ok, retry = guardrails.limiter.allow(who)
        if not ok:
            log.warning("rate limit hit: %s on %s", who, request.url.path)
            return JSONResponse({"ok": False, "reason": "rate_limited", "retry_after": retry},
                                status_code=429, headers={"Retry-After": str(retry)})
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s -> unhandled error (%.0f ms)", request.method,
                      request.url.path, (time.perf_counter() - t0) * 1000)
        raise
    ms = (time.perf_counter() - t0) * 1000
    # streamed responses (SSE, media) log time-to-first-byte, not total duration
    emit = log.warning if response.status_code >= 500 else log.info
    emit("%s %s -> %d (%.0f ms)", request.method, request.url.path,
         response.status_code, ms)
    return response


@app.middleware("http")          # registered last = runs first: identity before everything
async def visitor_identity(request: Request, call_next):
    """Every browser gets its own private guest profile (helper/identity.py)."""
    new = identity.assign(request) if request.url.path.startswith("/api") else None
    response = await call_next(request)
    if new:
        identity.remember(request, response, new)
    return response


# Built frontend (vite build) served from the same origin as /api, so cookies, SSE and
# the OAuth return URL need no CORS setup. Docker copies it to ./frontend_dist; a
# plain checkout builds into frontend/dist. Registered last: /api routes win.
_ROOT = Path(__file__).resolve().parent.parent
_DIST = next((d for d in (_ROOT / "frontend_dist", _ROOT / "frontend" / "dist")
              if (d / "index.html").exists()), None)
if _DIST:
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")

log.info("Recal backend ready: mode=%s, firestore=%s",
         "mock" if settings.MOCK else "real",
         "on" if settings.GOOGLE_CLOUD_PROJECT else "off")
