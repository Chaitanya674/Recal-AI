"""Reel media: narration audio for the in-app player, and MP4 / PDF-slides renders."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ..database import fire
from ..features import render
from .chat_router import current_user

router = APIRouter(prefix="/api/reels")
_FILES = re.compile(r"scene\d{1,2}\.wav|reel\.mp4|reel\.pdf")


def _owned(request: Request, gen_id: str) -> dict:
    try:
        pkg = render.load_package(gen_id)
    except ValueError:
        pkg = None
    if not pkg or pkg.get("user") != current_user(request):
        raise HTTPException(404, "reel not found")
    return pkg


@router.post("/{gen_id}/render")
def render_reel(gen_id: str, body: dict, request: Request):
    """{"format": "mp4" | "pdf"} -> renders once (cached) and returns the download URL."""
    _owned(request, gen_id)
    fmt = body.get("format")
    if fmt not in ("mp4", "pdf"):
        raise HTTPException(400, "format must be mp4 or pdf")
    t0 = time.time()
    path = render.render_mp4(gen_id) if fmt == "mp4" else render.render_pdf(gen_id)
    return {"ok": True, "format": fmt, "url": f"/api/reels/{gen_id}/files/{path.name}",
            "seconds": round(time.time() - t0, 1), "bytes": path.stat().st_size}


@router.get("/{gen_id}/files/{name}")
def reel_file(gen_id: str, name: str, request: Request, inline: bool = False):
    """inline=1 skips the attachment disposition so the in-app player can stream it."""
    pkg = _owned(request, gen_id)
    if not _FILES.fullmatch(name):
        raise HTTPException(404, "file not found")
    mime = {"wav": "audio/wav", "mp4": "video/mp4", "pdf": "application/pdf"}[name.rsplit(".", 1)[1]]
    slug = re.sub(r"[^a-z0-9]+", "-", pkg.get("title", "reel").lower()).strip("-")[:50] or "reel"
    filename = "" if (name.endswith(".wav") or inline) else f"recal-{slug}.{name.rsplit('.', 1)[1]}"
    path = render.package_dir(gen_id) / name
    if path.exists():
        return FileResponse(path, media_type=mime, filename=filename or None)
    if fire.media_enabled():                     # another instance rendered it, or we restarted
        rel = render.gcs_rel(pkg, gen_id, name)
        if fire.blob_exists(rel):
            try:
                return RedirectResponse(fire.blob_signed_url(rel, filename=filename))
            except Exception:  # noqa: BLE001 — local creds often can't sign; proxy instead
                data = fire.blob_download(rel)
                if data:
                    headers = ({"Content-Disposition": f'attachment; filename="{filename}"'}
                               if filename else {})
                    return Response(data, media_type=mime, headers=headers)
    raise HTTPException(404, "file not found")
