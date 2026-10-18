"""Reel -> MP4 (motion graphics + narration) and PDF slides, rendered server-side.

Pillow draws the frames; imageio-ffmpeg's bundled ffmpeg encodes H.264/AAC. Both are
pip wheels: no system install, no GPU, no paid API. Scenes keep gentle motion for their
whole duration (drift, pops, self-drawing connectors), so every frame is drawn; the
prerender thread hides the encode time and mpdecimate still prunes any duplicates.
This is the ONLY renderer — the app's ReelPlayer just plays the MP4 produced here.
The pictorial scenes (title, keyword_match, flow with icons and carries,
vector_space, compare_two, callout) live in reel_engine and are drawn supersampled
(RENDER_SSAA, default 2) for anti-aliased edges; the remaining archetypes below are
drawn directly. Captions highlight each word as it is spoken.

Reel packages live in media/<generation_id>/ (reel.json + scene WAVs), written by
motion.make_reel; renders are cached next to them (reel.mp4, reel.pdf)."""
from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import reel_engine
from ..database import fire, schema

W, H, FPS = 1280, 720, 24
SSAA = max(1, int(os.getenv("RENDER_SSAA", "2")))
AUDIO_RATE = 24000                       # matches llm.TTS_RATE (16-bit mono PCM)
BG_CENTER, BG_EDGE = (23, 41, 90), (8, 15, 36)
INK, SUB, ACCENT, DARK = (238, 242, 250), (154, 168, 199), (245, 165, 36), (26, 18, 5)
NODE, NODE_LINE, CODE_BG, CODE_INK = (36, 52, 94), (58, 79, 134), (10, 15, 30), (214, 222, 238)

MEDIA = Path(__file__).resolve().parent.parent / "media"
_GEN_ID = re.compile(r"[0-9a-f]{12}")


# ---- reel packages ---------------------------------------------------------------
# Metadata lives in Firestore (`reels/{gen_id}`) and binaries in Cloud Storage when
# configured; the local folder is then only a scratch cache for ffmpeg and re-renders.
def package_dir(gen_id: str) -> Path:
    if not _GEN_ID.fullmatch(gen_id or ""):
        raise ValueError("bad generation id")
    return MEDIA / gen_id


def gcs_rel(pkg: dict, gen_id: str, name: str) -> str:
    return f"users/{pkg['user']}/reels/{gen_id}/{name}"


def save_package(gen_id: str, pkg: dict):
    d = package_dir(gen_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "reel.json").write_text(json.dumps(pkg), encoding="utf-8")
    if fire.enabled():
        try:
            fire.set_doc({**pkg, "meta": fire.meta()}, *schema.REELS, gen_id)
        except Exception:  # noqa: BLE001
            pass


def load_package(gen_id: str) -> dict | None:
    try:
        return json.loads((package_dir(gen_id) / "reel.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pass
    if fire.enabled():     # after a restart / on another instance
        try:
            pkg = fire.get_doc(*schema.REELS, gen_id)
            if pkg:
                pkg.pop("meta", None)
                return pkg
        except Exception:  # noqa: BLE001
            pass
    return None


def _ensure_local(gen_id: str, pkg: dict):
    """Pull narration WAVs from Cloud Storage into the scratch dir for ffmpeg."""
    d = package_dir(gen_id)
    d.mkdir(parents=True, exist_ok=True)
    if not fire.media_enabled():
        return
    for name in pkg.get("audio_files") or []:
        p = d / name
        if not p.exists():
            data = fire.blob_download(gcs_rel(pkg, gen_id, name))
            if data:
                p.write_bytes(data)


def _cached_render(gen_id: str, pkg: dict, name: str, mime: str) -> Path | None:
    """Local file, else the GCS copy (downloaded to scratch), else None."""
    out = package_dir(gen_id) / name
    if out.exists():
        return out
    if fire.media_enabled():
        data = fire.blob_download(gcs_rel(pkg, gen_id, name))
        if data:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            return out
    return None


def _publish(gen_id: str, pkg: dict, path: Path, mime: str):
    if fire.media_enabled():
        try:
            fire.blob_upload(gcs_rel(pkg, gen_id, path.name), path.read_bytes(), mime)
        except Exception:  # noqa: BLE001 — local copy still serves
            pass


def write_wav(path: Path, pcm: bytes):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(pcm)


# ---- drawing primitives ----------------------------------------------------------
# System fonts (Segoe on Windows, DejaVu on Linux) read far better than the bundled
# default; every chain falls back to Pillow's built-in so rendering never fails.
_FONT_FILES = {False: ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"),
               True: ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")}
_MONO_FILES = ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf")


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in _FONT_FILES[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)       # bundled scalable font (Pillow >= 10.1)


@lru_cache(maxsize=None)
def _mono(size: int) -> ImageFont.FreeTypeFont:
    for name in _MONO_FILES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return _font(size)


@lru_cache(maxsize=2)
def _background(ss: int = 1) -> Image.Image:
    """Radial glow centred slightly above the middle, fading to deep navy edges.
    The glow spans only ~15 brightness levels, so it is dithered: without the faint
    noise, H.264 turns each level step into a visible ring."""
    w, h = W * ss, H * ss
    grad = Image.radial_gradient("L").resize((int(w * 1.5), int(h * 1.5)), Image.BICUBIC)
    left, top = int(w * .75) - int(w * .5), int(h * .75) - int(h * .45)
    mask = grad.crop((left, top, left + w, top + h))          # radii .75w x .75h
    img = Image.composite(Image.new("RGB", (w, h), BG_EDGE),
                          Image.new("RGB", (w, h), BG_CENTER), mask)
    noise = Image.effect_noise((w, h), 3).convert("RGB")      # centred on 128
    return ImageChops.add(img, noise, scale=1, offset=-128)


_MOTES = [(r.random() * 1600, r.random() * 900, 1.2 + r.random() * 2, .12 + r.random() * .2,
           r.random() * 6) for r in [random.Random(70)] for _ in range(70)]


def _particles(d, t, ss: int = 1):
    """Slow-drifting dust motes behind the scene — the backdrop itself is alive."""
    k = W * ss / 1600
    for x0, y0, r, op, ph in _MOTES:
        x = ((x0 + math.sin(t * .12 + ph) * 14 + t * 3) % 1600) * k
        y = (y0 + math.cos(t * .1 + ph) * 10) * k
        d.ellipse((x - r * k, y - r * k, x + r * k, y + r * k), fill=_rgba((138, 160, 214), op))


def _panel(d, box, radius, fill, outline, a, width=2):
    """Card with drop shadow and a top bevel highlight — cheap depth."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 + 3, y0 + 7, x1 + 3, y1 + 7), radius, fill=_rgba((3, 6, 14), 0.5 * a))
    d.rounded_rectangle(box, radius, fill=_rgba(fill, a), outline=_rgba(outline, a), width=width)
    d.line((x0 + radius, y0 + 2, x1 - radius, y0 + 2), fill=_rgba((255, 255, 255), 0.10 * a), width=1)


def _rgba(color: tuple, alpha: float) -> tuple:
    return (*color[:3], max(0, min(255, int(255 * alpha))))


def _wrap(text: str, font, max_w: int, max_lines: int = 4) -> list[str]:
    lines, cur = [], ""
    for word in str(text).split():
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;") + "…"
    return lines


def _text_center(d, lines, font, cx, top, fill, gap=8, bold=False) -> int:
    """Draw centred lines starting at `top`; returns the y below the block."""
    y = top
    for line in lines:
        w = font.getlength(line)
        d.text((cx - w / 2, y), line, font=font, fill=fill,
               stroke_width=1 if bold else 0, stroke_fill=fill)
        y += font.size + gap
    return y


def _block_height(lines, font, gap=8) -> int:
    return len(lines) * (font.size + gap) - gap if lines else 0


def _ease(t: float, dur: float = 0.45) -> float:
    p = max(0.0, min(1.0, t / dur))
    return 1 - (1 - p) ** 3


def _ease_back(t: float, dur: float = 0.5) -> float:
    """Ease-out with a small overshoot — elements 'pop' into place."""
    p = max(0.0, min(1.0, t / dur))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _drift(t: float, phase: float = 0.0, amp: float = 3.0) -> float:
    """Perpetual gentle float: the canvas is never fully static."""
    return amp * math.sin(0.8 * t + phase)


def _arrow(d, ax, ay, bx, by, prog: float, color, width: int = 4):
    """A line that draws itself from (ax,ay) toward (bx,by); arrowhead lands at the end."""
    ex, ey = ax + (bx - ax) * prog, ay + (by - ay) * prog
    d.line((ax, ay, ex, ey), fill=color, width=width)
    if prog >= 0.97:
        ang = math.atan2(by - ay, bx - ax)
        pts = [(bx, by)] + [(bx - 15 * math.cos(ang + o), by - 15 * math.sin(ang + o))
                            for o in (-0.45, 0.45)]
        d.polygon(pts, fill=color)


def _boxes_in_rows(labels, font, max_w, pad=28, gap=22, box_h=64):
    """Lay out label boxes in centred, wrapped rows. Returns [(x0, y0, x1, y1, label)]."""
    rows, row, row_w = [], [], 0
    for label in labels:
        w = min(max_w, int(font.getlength(label)) + 2 * pad)
        if row and row_w + gap + w > max_w:
            rows.append((row, row_w))
            row, row_w = [], 0
        row_w += (gap if row else 0) + w
        row.append((label, w))
    if row:
        rows.append((row, row_w))
    total_h = len(rows) * box_h + (len(rows) - 1) * gap
    y = (H - 90 - total_h) / 2
    out = []
    for row, row_w in rows:
        x = (W - row_w) / 2
        for label, w in row:
            out.append((x, y, x + w, y + box_h, label))
            x += w + gap
        y += box_h + gap
    return out


# ---- archetypes (t = seconds into the scene; every frame keeps gentle motion) --------
def _build_diagram(d, s, t):
    nodes = s.get("nodes") or []
    order = s.get("order") or [n.get("id") for n in nodes]
    font = _font(28)
    labels = [str(n.get("label", "")) for n in nodes]
    boxes = _boxes_in_rows(labels, font, int(W * 0.9))
    dy = _drift(t)
    placed = []                                      # (k, box) with per-node pop lift
    for i, (x0, y0, x1, y1, label) in enumerate(boxes):
        nid = nodes[i].get("id")
        k = order.index(nid) if nid in order else i
        lift = (1 - min(_ease_back(t - 0.45 * k, 0.45), 1.0)) * 18
        placed.append((k, (x0, y0 + dy + lift, x1, y1 + dy + lift), label))
    centers = {k: ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for k, b, _ in placed}
    for k in sorted(centers)[:-1]:                   # connectors draw themselves, in order
        prog = _ease(t - 0.45 * (k + 1) - 0.1, 0.4)
        if prog > 0 and k + 1 in centers:
            (ax, ay), (bx, by) = centers[k], centers[k + 1]
            _arrow(d, ax, ay, bx, by, prog, _rgba(ACCENT, 0.85))
    for k, box, label in placed:                     # boxes over the connector lines
        a = _ease(t - 0.45 * k, 0.35)
        _panel(d, box, 10, NODE, NODE_LINE, a)
        _text_center(d, [label], font, (box[0] + box[2]) / 2,
                     box[1] + (box[3] - box[1] - font.size) / 2 - 2, _rgba(INK, a))


def _timeline(d, s, t):
    steps = [f"{i + 1}. {st}" for i, st in enumerate(s.get("steps") or [])]
    font = _font(26)
    boxes = _boxes_in_rows(steps, font, int(W * 0.92), pad=22, box_h=58)
    dy = _drift(t, 1.3)
    for i, (x0, y0, x1, y1, label) in enumerate(boxes):
        a = _ease(t - 0.35 * i, 0.3)
        lift = (1 - min(_ease_back(t - 0.35 * i, 0.4), 1.0)) * 16
        box = (x0, y0 + dy + lift, x1, y1 + dy + lift)
        last = i == len(boxes) - 1
        if i and abs(boxes[i - 1][1] - y0) < 1:      # same row: connector draws across
            prog = _ease(t - 0.35 * i - 0.05, 0.3)
            if prog > 0:
                py = (box[1] + box[3]) / 2
                _arrow(d, boxes[i - 1][2] + 2, py, x0 - 2, py, prog, _rgba(ACCENT, 0.8), width=3)
        _panel(d, box, 10, ACCENT if last else NODE, ACCENT if last else NODE_LINE, a)
        _text_center(d, [label], font, (x0 + x1) / 2, box[1] + (box[3] - box[1] - font.size) / 2 - 2,
                     _rgba(DARK if last else INK, a))


def _code_reveal(d, s, t):
    lines = [str(ln) for ln in (s.get("lines") or [])][:10]
    hl = set(s.get("highlights") or [])
    font = _mono(24)
    box_w = int(W * 0.78)
    h = len(lines) * (font.size + 10) + 40 + 28           # +28: editor-chrome header strip
    x0, y0 = (W - box_w) / 2, (H - 90 - h) / 2 + _drift(t, 0.4, 2.0)
    _panel(d, (x0, y0, x0 + box_w, y0 + h), 12, CODE_BG, NODE_LINE, 1, width=1)
    for j, c in enumerate(((246, 97, 81), (248, 195, 70), (97, 198, 115))):   # window dots
        d.ellipse((x0 + 20 + j * 24, y0 + 12, x0 + 34 + j * 24, y0 + 26), fill=_rgba(c, 0.9))
    for i, line in enumerate(lines):
        a = _ease(t - 0.15 * i, 0.2)
        y = y0 + 48 + i * (font.size + 10)
        if i in hl:                                  # highlights breathe once revealed
            pulse = 0.18 + 0.07 * math.sin(2.2 * t) * min(1.0, a)
            d.rectangle((x0 + 10, y - 4, x0 + box_w - 10, y + font.size + 4),
                        fill=_rgba(ACCENT, pulse * a))
        d.text((x0 + 28, y), line, font=font, fill=_rgba(CODE_INK, a))


def _counter(d, s, t):
    lo, hi = float(s.get("from", s.get("from_", 0)) or 0), float(s.get("to", 0) or 0)
    p = _ease(t, 1.2)
    v = lo + (hi - lo) * p
    num = f"{v:,.0f}" if lo.is_integer() and hi.is_integer() else f"{v:,.1f}"
    text = f"{num}{s.get('unit', '')}"
    final = f"{(f'{hi:,.0f}' if lo.is_integer() and hi.is_integer() else f'{hi:,.1f}')}{s.get('unit', '')}"
    big, small = _font(110, bold=True), _font(30)
    while big.size > 56 and big.getlength(final) > 330:     # long values shrink to fit the ring
        big = _font(big.size - 6, bold=True)
    label = _wrap(s.get("label", ""), small, int(W * 0.7), 2)
    r0 = max(140, big.getlength(final) / 2 + 40)            # the ring always clears the number
    block = 2 * r0 + 28 + _block_height(label, small)
    cy = (H - 90 - block) / 2 + r0 + _drift(t, 0.6, 2.0)
    r = r0 + (4 * math.sin(2.0 * (t - 1.2)) if t > 1.2 else 0)   # ring pulses when done
    d.arc((W / 2 - r, cy - r, W / 2 + r, cy + r), start=-90, end=-90 + 360 * p,
          fill=_rgba(ACCENT, 0.9), width=7)
    tip = math.radians(-90 + 360 * p)                 # glowing dot rides the ring's tip
    tx, ty = W / 2 + r * math.cos(tip), cy + r * math.sin(tip)
    d.ellipse((tx - 12, ty - 12, tx + 12, ty + 12), fill=_rgba(ACCENT, 0.3))
    d.ellipse((tx - 6, ty - 6, tx + 6, ty + 6), fill=_rgba(ACCENT, 1))
    d.text((W / 2, cy), text, font=big, fill=_rgba(INK, 1), anchor="mm")
    _text_center(d, label, small, W / 2, cy + r0 + 28, _rgba(SUB, 1))


def _bar_chart(d, s, t):
    """Score/ranking bars that grow with their values counting up; the highlighted
    bar is accented and a sheen keeps the chart alive after it settles."""
    bars = [b for b in (s.get("bars") or []) if isinstance(b, dict)][:6]
    if not bars:
        return
    unit, hi = str(s.get("unit", "")), str(s.get("highlight", "")).lower()
    lab_f, val_f = _font(26), _font(24)
    vmax = max(abs(float(b.get("value", 0) or 0)) for b in bars) or 1.0
    dy = _drift(t, 0.5, 2.0)
    row_h, gap = 56, 18
    total_h = len(bars) * row_h + (len(bars) - 1) * gap
    top = (H - 90 - total_h) / 2 + dy
    span = int(W * 0.8)
    x0 = (W - span) / 2
    lab_w = min(300, max(lab_f.getlength(str(b.get("label", ""))) for b in bars) + 10)
    bar_x = x0 + lab_w + 20
    bar_max = x0 + span - bar_x - 120
    d.line((bar_x - 2, top - 10, bar_x - 2, top + total_h + 10),
           fill=_rgba(NODE_LINE, 0.9), width=2)
    for i, b in enumerate(bars):
        label, value = str(b.get("label", "")), float(b.get("value", 0) or 0)
        a = _ease(t - 0.2 * i, 0.3)
        grow = _ease(t - 0.2 * i - 0.1, 0.9)
        y = top + i * (row_h + gap)
        accent = bool(hi) and hi in label.lower()
        w = bar_max * (abs(value) / vmax) * grow
        d.text((bar_x - 22 - lab_f.getlength(label), y + (row_h - lab_f.size) / 2),
               label, font=lab_f, fill=_rgba(INK, a))
        _panel(d, (bar_x, y + 8, bar_x + max(w, 4), y + row_h - 8), 8,
               ACCENT if accent else NODE, ACCENT if accent else NODE_LINE, a)
        if w > 36:                       # moving sheen so settled bars still live
            sx = bar_x + 8 + (w - 20) * ((t * 0.45 + i * 0.2) % 1.0)
            d.rectangle((sx, y + 10, sx + 10, y + row_h - 10), fill=_rgba(INK, 0.12 * a))
        v = value * grow
        num = f"{v:,.0f}" if float(value).is_integer() else f"{v:,.1f}"
        d.text((bar_x + max(w, 4) + 14, y + (row_h - val_f.size) / 2),
               f"{num}{unit}", font=val_f, fill=_rgba(ACCENT if accent else SUB, a))


def _bezier_pts(p0, p1, p2, prog: float, steps: int = 48) -> list[tuple]:
    """Points along a quadratic bezier, drawn up to `prog` of its length."""
    pts = []
    for i in range(max(2, int(steps * prog)) + 1):
        u = prog * i / max(2, int(steps * prog))
        a, b = (1 - u) ** 2, 2 * (1 - u) * u
        pts.append((a * p0[0] + b * p1[0] + u * u * p2[0],
                    a * p0[1] + b * p1[1] + u * u * p2[1]))
    return pts


def _token_attention(d, s, t):
    """A sentence of token chips; attention arcs fan out of the QUERY chip and land
    on every other token, thickness and glow = weight. Pulses ride the strongest arc."""
    tokens = [str(x) for x in (s.get("tokens") or [])][:8]
    if len(tokens) < 2:
        return
    qi = max(0, min(int(s.get("query") or 0), len(tokens) - 1))
    ws = [max(0.0, min(1.0, float(w or 0))) for w in (s.get("weights") or [])]
    ws += [0.0] * (len(tokens) - len(ws))
    font, wf = _font(28), _font(19)
    pad, gap, box_h = 22, 16, 58
    widths = [min(220, max(64, int(font.getlength(tok)) + 2 * pad)) for tok in tokens]
    row_w = sum(widths) + gap * (len(tokens) - 1)
    x = (W - row_w) / 2
    y0 = H - 90 - 200 + _drift(t, 0.7, 2.0)
    tops, boxes = [], []
    for i, (tok, bw) in enumerate(zip(tokens, widths)):
        lift = (1 - min(_ease_back(t - 0.18 * i, 0.4), 1.0)) * 16
        boxes.append((x, y0 + lift, x + bw, y0 + box_h + lift))
        tops.append((x + bw / 2, y0 + lift))
        x += bw + gap
    order = sorted((j for j in range(len(tokens)) if j != qi),
                   key=lambda j: ws[j], reverse=True)
    top_keys = [j for j in order[:2] if ws[j] > 0.05]
    for rank, j in enumerate(order):                 # arcs: strongest first, border-to-border
        if ws[j] <= 0.02:
            continue
        prog = _ease(t - 0.7 - 0.3 * rank, 0.6)
        if prog <= 0:
            continue
        p0, p2 = tops[qi], tops[j]
        apex = (-90 - 150 * abs(j - qi) / len(tokens))
        p1 = ((p0[0] + p2[0]) / 2, min(p0[1], p2[1]) + apex)
        a = 0.3 + 0.65 * ws[j]
        pts = _bezier_pts(p0, p1, p2, prog)
        d.line(pts, fill=_rgba(ACCENT if j in top_keys else NODE_LINE, a),
               width=2 + int(5 * ws[j]), joint="curve")
        if prog >= 1 and j in top_keys:              # weight label + riding pulse
            mx, my = _bezier_pts(p0, p1, p2, 1.0)[24]
            d.text((mx - wf.getlength(f"{ws[j]:.2f}") / 2, my - 26), f"{ws[j]:.2f}",
                   font=wf, fill=_rgba(ACCENT, 1))
            p = (t * 0.5 + 0.3 * rank) % 1.0
            px, py = _bezier_pts(p0, p1, p2, 1.0)[int(48 * p)]
            d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=_rgba(ACCENT, 0.95))
    for i, box in enumerate(boxes):                  # chips over the arc tails
        a = _ease(t - 0.18 * i, 0.35)
        strongest = top_keys and i == top_keys[0]
        _panel(d, box, 10, ACCENT if i == qi else NODE,
               ACCENT if strongest or i == qi else NODE_LINE, a)
        _text_center(d, [tokens[i]], font, (box[0] + box[2]) / 2,
                     box[1] + (box[3] - box[1] - font.size) / 2 - 2,
                     _rgba(DARK if i == qi else INK, a))
        tag = "query" if i == qi else ("key" if strongest else "")
        if tag:
            d.text(((box[0] + box[2]) / 2 - wf.getlength(tag) / 2, box[3] + 8),
                   tag, font=wf, fill=_rgba(ACCENT, a))


def _matrix_heat(d, s, t):
    """Heatmap grid that fills cell by cell, intensity = value; the strongest cell
    carries its number and breathes. The formula/caption sits beside the grid."""
    labels = [str(x)[:10] for x in (s.get("labels") or [])][:6]
    n = len(labels)
    if n < 2:
        return
    rows = s.get("cells") or []

    def val(r, c):
        try:
            return max(0.0, min(1.0, float(rows[r][c])))
        except (IndexError, TypeError, ValueError):
            return 0.0

    lf = _font(20)
    cell = min(76, int(400 / n))
    gw = n * cell + (n - 1) * 6
    lab_w = max(lf.getlength(lb) for lb in labels) + 18
    x0 = max(70, (W * 0.56 - gw) / 2 + lab_w / 2)
    y0 = (H - 90 - gw - 40) / 2 + 40 + _drift(t, 1.1, 2.0)
    best = max(((val(r, c), r, c) for r in range(n) for c in range(n)),
               default=(0, 0, 0))
    for c, lb in enumerate(labels):                  # column labels above
        a = _ease(t - 0.05 * c, 0.3)
        cx = x0 + c * (cell + 6) + cell / 2
        d.text((cx - lf.getlength(lb) / 2, y0 - 30), lb, font=lf, fill=_rgba(SUB, a))
    for r in range(n):
        ly = y0 + r * (cell + 6)
        a = _ease(t - 0.05 * r, 0.3)
        d.text((x0 - 12 - lf.getlength(labels[r]), ly + cell / 2 - lf.size / 2),
               labels[r], font=lf, fill=_rgba(SUB, a))
        for c in range(n):
            v = val(r, c)
            a = _ease(t - 0.09 * (r * n + c) - 0.2, 0.3)
            box = (x0 + c * (cell + 6), ly, x0 + c * (cell + 6) + cell, ly + cell)
            d.rounded_rectangle(box, 8, fill=_rgba(NODE, 0.75 * a),
                                outline=_rgba(NODE_LINE, 0.6 * a))
            if v > 0.02:
                glow = 1.0 if (r, c) != best[1:] else 0.85 + 0.15 * math.sin(2.2 * t)
                d.rounded_rectangle(box, 8, fill=_rgba(ACCENT, (0.12 + 0.75 * v) * a * glow))
            if (r, c) == best[1:] and best[0] > 0:
                vf = _font(22)
                d.text((box[0] + cell / 2 - vf.getlength(f"{v:.2f}") / 2,
                        box[1] + cell / 2 - vf.size / 2), f"{v:.2f}",
                       font=vf, fill=_rgba(DARK, a))
    cap = str(s.get("caption", ""))
    if cap:
        cf = _mono(26)
        lines = _wrap(cap, cf, int(W * 0.3), 3)
        cw = max(cf.getlength(ln) for ln in lines) + 56
        ch = _block_height(lines, cf, 10) + 48
        cx0 = min(W - cw - 50, x0 + gw + 70)
        cy0 = y0 + gw / 2 - ch / 2
        a = _ease(t - 0.8, 0.5)
        _panel(d, (cx0, cy0, cx0 + cw, cy0 + ch), 12, CODE_BG, NODE_LINE, a, width=1)
        _text_center(d, lines, cf, cx0 + cw / 2, cy0 + 24, _rgba(CODE_INK, a), gap=10)


def _cycle(d, s, t):
    """Stages marching forward, then a dashed arc loops the output back to the
    start — iterative/autoregressive processes, data visibly going around."""
    steps = [str(x) for x in (s.get("steps") or [])][:5]
    if not steps:
        return
    font, lf = _font(27), _font(21)
    pad, gap, box_h = 24, 0, 60
    widths = [min(260, int(font.getlength(st)) + 2 * pad) for st in steps]
    usable = int(W * 0.9)
    gap = max(56, min((usable - sum(widths)) / max(len(steps) - 1, 1), 160))
    row_w = sum(widths) + gap * (len(steps) - 1)
    x = (W - row_w) / 2
    y0 = (H - 90 - box_h) / 2 - 50 + _drift(t)
    boxes = []
    for bw in widths:
        boxes.append((x, y0, x + bw, y0 + box_h))
        x += bw + gap
    for i, (box, st) in enumerate(zip(boxes, steps)):   # boxes pop, arrows chase them
        if i:
            prog = _ease(t - 0.3 * i - 0.05, 0.3)
            if prog > 0:
                py = y0 + box_h / 2
                _arrow(d, boxes[i - 1][2] + 4, py, box[0] - 4, py, prog,
                       _rgba(NODE_LINE, 0.95), width=3)
        a = _ease(t - 0.3 * i, 0.35)
        last = i == len(steps) - 1
        lift = (1 - min(_ease_back(t - 0.3 * i, 0.4), 1.0)) * 14
        b = (box[0], box[1] + lift, box[2], box[3] + lift)
        _panel(d, b, 10, ACCENT if last else NODE, ACCENT if last else NODE_LINE, a)
        _text_center(d, [st], font, (b[0] + b[2]) / 2,
                     b[1] + (b[3] - b[1] - font.size) / 2 - 2,
                     _rgba(DARK if last else INK, a))
    start = 0.3 * len(steps) + 0.2                      # loop-back: marching dashes
    prog = _ease(t - start, 0.8)
    if prog > 0:
        p0 = ((boxes[-1][0] + boxes[-1][2]) / 2, boxes[-1][3] + 6)
        p2 = ((boxes[0][0] + boxes[0][2]) / 2, boxes[0][3] + 6)
        p1 = ((p0[0] + p2[0]) / 2, p0[1] + 150)
        pts = _bezier_pts(p0, p1, p2, prog, steps=72)
        offset = (t * 36) % 26
        run = -offset
        for i in range(1, len(pts)):
            seg = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            if (run % 26) < 16:
                d.line((pts[i - 1], pts[i]), fill=_rgba(ACCENT, 0.9), width=4)
            run += seg
        if prog >= 0.97:
            ang = math.atan2(pts[-1][1] - pts[-8][1], pts[-1][0] - pts[-8][0])
            tip = [(pts[-1])] + [(pts[-1][0] - 15 * math.cos(ang + o),
                                  pts[-1][1] - 15 * math.sin(ang + o)) for o in (-0.45, 0.45)]
            d.polygon(tip, fill=_rgba(ACCENT, 0.9))
        label = str(s.get("loop_label", ""))
        if label and prog > 0.4:
            d.text((W / 2 - lf.getlength(label) / 2, p1[1] - 36 + 14),
                   label, font=lf, fill=_rgba(SUB, _ease(t - start - 0.4, 0.4)))


# drawn directly; title / keyword_match / flow / vector_space / compare_two / callout
# are the pictorial scenes in reel_engine.SCENES
_ARCHETYPES = {"build_diagram": _build_diagram, "timeline": _timeline,
               "code_reveal": _code_reveal, "counter": _counter, "bar_chart": _bar_chart,
               "token_attention": _token_attention, "matrix_heat": _matrix_heat,
               "cycle": _cycle}


@lru_cache(maxsize=512)
def _caption_layer(caption: str, spoken: int) -> tuple[Image.Image, int]:
    """Caption bar with the words already spoken lit and the rest dimmed.
    Returns (RGBA strip, y offset); cached per (caption, words spoken)."""
    font = _font(26)
    words = caption.split()
    lines, cur = [], []
    for i, w in enumerate(words):
        if cur and font.getlength(" ".join(x for _, x in cur + [(i, w)])) > W - 140:
            lines.append(cur)
            cur = []
        cur.append((i, w))
    if cur:
        lines.append(cur)
    lines = lines[:2]
    line_h = font.size + 6
    h = len(lines) * line_h - 6 + 26
    strip = Image.new("RGBA", (W, h + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    d.rounded_rectangle((26, 0, W - 26, h), 12, fill=(5, 9, 22, 214))
    space = font.getlength(" ")
    for li, line in enumerate(lines):
        width = font.getlength(" ".join(w for _, w in line))
        x, y = (W - width) / 2, 13 + li * line_h
        for i, w in line:
            d.text((x, y), w, font=font, fill=(238, 242, 252, 255 if i < spoken else 102))
            x += font.getlength(w) + space
    return strip, H - h - 18


def _spoken(caption: str, t: float, dur: float | None) -> int:
    """Words said by time t: narration spread over the scene, minus the trailing breath."""
    n = len(caption.split())
    if not dur:
        return n
    return max(0, min(n, int((t - .3) / max(1.0, dur - 1.1) * n) + 1)) if t >= .3 else 0


def frame(scene: dict, t: float, caption: str = "", dur: float | None = None) -> Image.Image:
    """Frames are RGB so ImageDraw's "RGBA" mode alpha-blends every translucent shape."""
    arch, slots = scene.get("archetype"), scene.get("slots") or {}
    base = _background(1).copy()
    _particles(ImageDraw.Draw(base, "RGBA"), t)
    if arch in _ARCHETYPES:                                        # directly drawn scenes
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _ARCHETYPES[arch](ImageDraw.Draw(layer), slots, t)
        img = base.copy()
        img.paste(layer, (0, int((1 - _ease(t, 0.3)) * 16)), layer)
    else:                                                          # pictorial, supersampled
        img = _background(SSAA).copy()
        d = ImageDraw.Draw(img, "RGBA")
        _particles(d, t, SSAA)
        reel_engine.SCENES.get(arch, reel_engine.title)(
            reel_engine.Canvas(d, W * SSAA / reel_engine.LW, _font), slots, t, dur or 8.0)
        if SSAA > 1:
            img = img.reduce(SSAA)
    fade_in = 1 - (1 - min(1.0, max(0.0, t / .35))) ** 3
    fade_out = 1 - min(1.0, max(0.0, (t - (dur - .35)) / .35)) if dur else 1.0
    if min(fade_in, fade_out) < 1:
        img = Image.blend(base, img, min(fade_in, fade_out))
    if caption:
        strip, y = _caption_layer(caption, _spoken(caption, t, dur))
        img.paste(strip, (0, y), strip)
    if dur:                                     # video-style scene progress bar
        ImageDraw.Draw(img).rectangle((0, H - 5, W * max(0.0, min(t / dur, 1.0)), H), fill=ACCENT)
    return img


# ---- outputs -------------------------------------------------------------------------
def _narration_track(d: Path, pkg: dict) -> Path | None:
    """One WAV for the whole reel: each scene's clip padded with silence to its duration."""
    files = pkg.get("audio_files") or []
    if not files:
        return None
    out = d / "narration.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        for name, dur in zip(files, pkg["durations"]):
            with wave.open(str(d / name), "rb") as r:
                pcm = r.readframes(r.getnframes())
            size = int(dur * AUDIO_RATE) * 2
            w.writeframes(pcm[:size] + b"\0" * max(0, size - len(pcm)))
    return out


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(gen_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(gen_id, threading.Lock())


def prerender(gen_id: str):
    """Start the MP4 render in the background right after a reel is made, so the
    download button is usually instant. Failures surface on the real request."""
    def run():
        try:
            render_mp4(gen_id)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True, name=f"prerender-{gen_id}").start()


def render_mp4(gen_id: str) -> Path:
    with _lock(gen_id):          # a click during the background render waits for it
        return _render_mp4(gen_id)


def _render_mp4(gen_id: str) -> Path:
    """Only distinct frames are drawn (in parallel): each scene's animation frames plus its
    settled still. One ffmpeg call then reads them as per-scene 24 fps sequences, holds
    each still with the `loop` filter (exact frame counts, so narration stays in sync),
    and `mpdecimate` drops the held duplicates so x264 only encodes frames that change."""
    import imageio_ffmpeg
    d = package_dir(gen_id)
    pkg = load_package(gen_id)
    cached = _cached_render(gen_id, pkg, "reel.mp4", "video/mp4")
    if cached:
        return cached
    _ensure_local(gen_id, pkg)
    out = d / "reel.mp4"
    scenes = pkg["scene_script"]["scenes"]
    audio = _narration_track(d, pkg)
    work = d / "frames"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()

    plan, jobs = [], []          # every frame is drawn: scenes keep gentle motion for
    for s, (scene, dur) in enumerate(zip(scenes, pkg["durations"])):   # their whole duration
        total = max(1, round(dur * FPS))
        plan.append((total - 1, 0))
        jobs += [(s, n, n / FPS) for n in range(total)]

    def draw(job):
        s, n, t = job
        frame(scenes[s], t, scenes[s].get("narration", ""),
              dur=pkg["durations"][s]).save(work / f"s{s:02d}_{n:04d}.jpg", quality=95)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(draw, jobs))

    n = len(scenes)
    seconds = sum(animated + hold + 1 for animated, hold in plan) / FPS
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error"]
    for s in range(n):
        cmd += ["-framerate", str(FPS), "-i", f"s{s:02d}_%04d.jpg"]
    # narration, or silence: an audio track of the exact length keeps the container at the
    # right duration even though mpdecimate drops the final held frames
    cmd += ["-i", str(audio)] if audio else ["-f", "lavfi", "-t", f"{seconds:.3f}",
                                              "-i", f"anullsrc=r={AUDIO_RATE}:cl=mono"]
    graph = ";".join(f"[{s}:v]loop=loop={hold}:size=1:start={animated}[v{s}]"
                     for s, (animated, hold) in enumerate(plan))
    graph += (";" + "".join(f"[v{s}]" for s in range(n)) + f"concat=n={n}:v=1:a=0,"
              f"mpdecimate=hi=64:lo=64:frac=0:max={FPS // 2},"
              "scale=in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709,"
              "format=yuv420p[v]")          # JPEG frames are full-range BT.601; video wants TV-range BT.709
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", f"{n}:a", "-fps_mode", "vfr",
            "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", "128k"]
    tmp = d / "reel.part.mp4"
    cmd += ["-movflags", "+faststart", str(tmp)]
    done = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {done.stderr.strip()[-300:]}")
    tmp.replace(out)
    _publish(gen_id, pkg, out, "video/mp4")
    return out


def render_pdf(gen_id: str) -> Path:
    """One slide per scene (its settled frame), narration as the slide caption."""
    d = package_dir(gen_id)
    pkg = load_package(gen_id)
    cached = _cached_render(gen_id, pkg, "reel.pdf", "application/pdf")
    if cached:
        return cached
    d.mkdir(parents=True, exist_ok=True)
    out = d / "reel.pdf"
    pages = [frame(sc, 60.0, sc.get("narration", "")) for sc in pkg["scene_script"]["scenes"]]
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=96)
    _publish(gen_id, pkg, out, "application/pdf")
    return out
