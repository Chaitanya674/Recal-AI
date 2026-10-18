"""Pictorial reel scenes (engine v2), ported from the reference HTML engine.

Scenes are drawn in the reference's 1600x900 logical space through `Canvas`, which
scales onto the real frame (supersampled by render.py for anti-aliasing). Every
scene is a pure function of local time t, so the MP4 renderer can draw any frame.

What v2 adds over the box-and-arrow scenes:
- flow: nodes may name an `icon` (encoder, database, search graph…) drawn as an
  object that reacts when data arrives; edges may name a `carry` — the actual thing
  travelling the arrow (words, a number vector, a ranked list) — and branching
  pipelines are laid out as a layered graph (loops drawn as arcs over the top).
- keyword_match: query words drop onto a document and are checked one by one.
- vector_space: a query, its nearest neighbours, and labels that never collide.
- title / compare_two / callout: word-by-word reveals and staged comparisons.
Vector numbers shown on a carry are illustrative, derived from its text, so the
prompts never have to invent numbers."""
from __future__ import annotations

import math
import random
import zlib
from contextlib import contextmanager

LW, LH = 1600, 900

C = {"text": (238, 242, 252), "muted": (154, 171, 205), "dim": (97, 114, 154),
     "accent": (245, 165, 36), "blue": (91, 156, 240), "panel": (22, 36, 74),
     "panel2": (29, 47, 92), "edge": (45, 66, 116), "ink": (11, 20, 48),
     "good": (63, 211, 154), "bad": (255, 107, 107), "chip": (26, 43, 87),
     "vec": (15, 27, 61), "far": (127, 143, 179), "badink": (42, 11, 11)}


# ---------------------------------------------------------------------------
# math
# ---------------------------------------------------------------------------
def clamp(x, a=0.0, b=1.0):
    return min(b, max(a, x))


def seg(t, a, b):
    if b <= a:
        return 1.0 if t >= b else 0.0
    return clamp((t - a) / (b - a))


def e_out(t):
    return 1 - (1 - t) ** 3


def e_inout(t):
    return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2


def e_back(t):
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def lerp(a, b, t):
    return a + (b - a) * t


def bump(p):
    return math.sin(math.pi * clamp(p))


def mix(a, b, t):
    t = clamp(t)
    return tuple(round(lerp(x, y, t)) for x, y in zip(a, b))


def seeded(key: str):
    return random.Random(zlib.crc32(str(key).encode())).random


def peak(values, fn=lambda v: v):
    return max([fn(v) for v in values] or [0.0])


# ---------------------------------------------------------------------------
# canvas: logical 1600x900 space, transform + opacity stacks
# ---------------------------------------------------------------------------
class Canvas:
    def __init__(self, draw, k: float, font_fn):
        self.d, self.k, self._font_fn = draw, k, font_fn
        self._tf = [(0.0, 0.0, 1.0)]
        self._al = [1.0]

    @contextmanager
    def at(self, x, y, s=1.0):
        ox, oy, sc = self._tf[-1]
        self._tf.append((ox + x * sc, oy + y * sc, sc * s))
        try:
            yield
        finally:
            self._tf.pop()

    @contextmanager
    def fade(self, a):
        self._al.append(self._al[-1] * clamp(a))
        try:
            yield
        finally:
            self._al.pop()

    # -- conversions
    def _p(self, x, y):
        ox, oy, sc = self._tf[-1]
        return ((ox + x * sc) * self.k, (oy + y * sc) * self.k)

    def _len(self, v):
        return v * self._tf[-1][2] * self.k

    def _w(self, w):
        return max(1, round(self._len(w)))

    def _c(self, col, a=1.0):
        al = clamp(a * self._al[-1])
        return None if col is None or al <= 0.004 else (*col[:3], round(255 * al))

    def font(self, size, weight=400):
        return self._font_fn(max(6, round(self._len(size))), weight >= 600)

    def tw(self, s, size, weight=400):
        """Logical width of s, independent of the current transform."""
        return self._font_fn(max(6, round(size * self.k)), weight >= 600).getlength(str(s)) / self.k

    # -- primitives
    def rect(self, x, y, w, h, r=0, fill=None, stroke=None, sw=0, a=1.0):
        if w <= 0 or h <= 0:
            return
        f, o = self._c(fill, a), self._c(stroke, a)
        if not f and not o:
            return
        (x0, y0), (x1, y1) = self._p(x, y), self._p(x + w, y + h)
        rad = min(self._len(r), (x1 - x0) / 2, (y1 - y0) / 2)
        self.d.rounded_rectangle((x0, y0, x1, y1), max(0, rad), fill=f, outline=o,
                                 width=self._w(sw) if o else 0)

    def ellipse(self, cx, cy, rx, ry, fill=None, stroke=None, sw=0, a=1.0):
        f, o = self._c(fill, a), self._c(stroke, a)
        if (not f and not o) or rx <= 0 or ry <= 0:
            return
        (x0, y0), (x1, y1) = self._p(cx - rx, cy - ry), self._p(cx + rx, cy + ry)
        self.d.ellipse((x0, y0, x1, y1), fill=f, outline=o, width=self._w(sw) if o else 0)

    def circle(self, cx, cy, r, **kw):
        self.ellipse(cx, cy, r, r, **kw)

    def glow(self, cx, cy, r, col, a):
        """Soft halo: stacked translucent discs stand in for a blur filter."""
        if a <= 0.01:
            return
        for i in range(5):
            self.circle(cx, cy, r * (1.3 - i * 0.13), fill=col, a=a * 0.2)

    def line(self, pts, col, width=2, a=1.0, cap=False):
        c = self._c(col, a)
        if not c or len(pts) < 2:
            return
        px = [self._p(x, y) for x, y in pts]
        w = self._w(width)
        self.d.line(px, fill=c, width=w, joint="curve")
        if cap and w > 2:
            for x, y in (px[0], px[-1]):
                self.d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=c)

    def dashed(self, pts, col, width=2, dash=8, gap=7, a=1.0):
        path = Path(pts)
        pos = 0.0
        while pos < path.length:
            self.line(path.slice(pos, min(path.length, pos + dash)), col, width, a)
            pos += dash + gap

    def poly(self, pts, fill=None, stroke=None, sw=0, a=1.0):
        f, o = self._c(fill, a), self._c(stroke, a)
        if (not f and not o) or len(pts) < 3:
            return
        self.d.polygon([self._p(x, y) for x, y in pts], fill=f, outline=o,
                       width=self._w(sw) if o else 0)

    def text(self, x, y, s, size, col=None, a=1.0, weight=400, anchor="mm"):
        c = self._c(col or C["text"], a)
        if not c or not str(s):
            return
        self.d.text(self._p(x, y), str(s), font=self.font(size, weight), fill=c, anchor=anchor)


class Path:
    """Polyline with arc-length lookup (stands in for SVG getPointAtLength)."""

    def __init__(self, pts):
        self.pts = list(pts)
        self.cum = [0.0]
        for (x0, y0), (x1, y1) in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.cum[-1]

    @classmethod
    def cubic(cls, p0, p1, p2, p3, n=48):
        pts = []
        for i in range(n + 1):
            u = i / n
            a, b, c, d = (1 - u) ** 3, 3 * (1 - u) ** 2 * u, 3 * (1 - u) * u * u, u ** 3
            pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
        return cls(pts)

    def point(self, s):
        s = clamp(s, 0, self.length)
        for i in range(1, len(self.cum)):
            if self.cum[i] >= s:
                span = self.cum[i] - self.cum[i - 1] or 1
                u = (s - self.cum[i - 1]) / span
                (x0, y0), (x1, y1) = self.pts[i - 1], self.pts[i]
                return (lerp(x0, x1, u), lerp(y0, y1, u))
        return self.pts[-1]

    def slice(self, a, b):
        out = [self.point(a)]
        out += [p for p, c in zip(self.pts, self.cum) if a < c < b]
        out.append(self.point(b))
        return out


# ---------------------------------------------------------------------------
# measured text: wrap, shrink and ellipsize so nothing overflows
# ---------------------------------------------------------------------------
def wrap_lines(c, s, size, max_w, weight=400):
    lines, cur = [], ""
    for w in str(s).split():
        t = f"{cur} {w}" if cur else w
        if not cur or c.tw(t, size, weight) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def ellipsize(c, s, size, max_w, weight=400):
    s = str(s)
    if c.tw(s, size, weight) <= max_w:
        return s
    while len(s) > 1 and c.tw(s + "…", size, weight) > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def fit(c, s, max_w, size, min_size, max_lines, weight=400):
    z = size
    while True:
        lines = wrap_lines(c, s, z, max_w, weight)
        if (len(lines) <= max_lines and all(c.tw(ln, z, weight) <= max_w for ln in lines)) \
                or z <= min_size:
            break
        z -= 1
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = ellipsize(c, lines[-1] + " …", z, max_w, weight)
    return [ellipsize(c, ln, z, max_w, weight) for ln in lines], z


def multiline(c, lines, x, cy, size, col=None, weight=400, a=1.0):
    lh = size * 1.22
    y0 = cy - (len(lines) - 1) * lh / 2
    for i, ln in enumerate(lines):
        c.text(x, y0 + i * lh, ln, size, col, a, weight)


# ---------------------------------------------------------------------------
# icon library: each draws around (0,0); arrivals a / departures d are 0..1
# progress values, one per incoming / outgoing carry, in time order
# ---------------------------------------------------------------------------
def _i_query(c, a, d, node):
    c.rect(-72, -34, 144, 68, 34, fill=C["panel"], stroke=C["edge"], sw=3)
    c.rect(-50, -13, 44, 9, 4.5, fill=C["dim"])
    c.rect(-50, 3, 30, 9, 4.5, fill=C["dim"])
    with c.at(30, -4, 1 + .15 * peak(a + d, bump)):
        c.circle(0, 0, 15, stroke=C["accent"], sw=6)
        c.line([(11, 11), (24, 24)], C["accent"], 7, cap=True)


def _i_document(c, a, d, node):
    c.poly([(-44, -58), (18, -58), (44, -32), (44, 58), (-44, 58)], fill=C["panel"],
           stroke=C["blue"], sw=3)
    c.line([(18, -58), (18, -32), (44, -32)], C["blue"], 3)
    p = peak(d)
    for i, (x, y, w) in enumerate([(-30, -32, 34), (-30, -14, 62), (-30, 2, 52),
                                   (-30, 18, 62), (-30, 34, 40)]):
        c.rect(x, y, w, 7, 3.5, fill=mix(C["dim"], C["accent"], clamp(p * 7 - i)))


def _i_index(c, a, d, node):
    c.rect(-64, -54, 128, 108, 12, fill=C["panel"], stroke=C["blue"], sw=3)
    c.line([(-6, -44), (-6, 44)], C["edge"], 2)
    p = a[-1] if a else 0
    for i in range(4):
        y = -40 + i * 22
        with c.fade(.5 + .5 * bump(p * 1.6 - i * .18)):
            c.rect(-52, y, 38, 12, 6, fill=C["blue"])
            for j in range(3):
                c.circle(8 + j * 16, y + 6, 4.5, fill=C["muted"])


_ENC_COLS = [(-42, [-26, 0, 26]), (0, [-36, -12, 12, 36]), (42, [-26, 0, 26])]


def _i_encoder(c, a, d, node):
    c.rect(-70, -54, 140, 108, 18, fill=C["panel"], stroke=C["blue"], sw=3)
    p = a[-1] if a else 0
    for ci in range(2):
        col = mix(C["edge"], C["accent"], clamp(p * 3.4 - ci * 1.05 - .5) * .8)
        for y1 in _ENC_COLS[ci][1]:
            for y2 in _ENC_COLS[ci + 1][1]:
                c.line([(_ENC_COLS[ci][0], y1), (_ENC_COLS[ci + 1][0], y2)], col, 1.6)
    for i, (x, ys) in enumerate(_ENC_COLS):
        for y in ys:
            c.circle(x, y, 7.5, fill=mix(C["ink"], C["accent"], clamp(p * 3.4 - i * 1.05)),
                     stroke=C["blue"], sw=2.5)


def _i_model(c, a, d, node):
    c.rect(-46, -46, 92, 92, 14, fill=C["panel"], stroke=C["blue"], sw=3)
    for i in range(3):
        o = -24 + i * 24
        for seg_ in ([(o, -46), (o, -60)], [(o, 46), (o, 60)], [(-46, o), (-60, o)], [(46, o), (60, o)]):
            c.line(seg_, C["blue"], 4, cap=True)
    c.rect(-20, -20, 40, 40, 8, fill=mix(C["dim"], C["accent"], peak(a, bump)))


_DB_ROWS = []
for _i in range(5):
    _r = seeded(f"row{_i}")
    _DB_ROWS.append([_r() * 2 - 1 for _ in range(6)])


def _i_database(c, a, d, node):
    rx, ry, top, bot, base = 60, 17, -48, 48, 2
    c.ellipse(0, bot, rx, ry, fill=C["panel"], stroke=C["blue"], sw=3)
    c.rect(-rx, top, 2 * rx, bot - top, fill=C["panel"])
    c.line([(-rx, top), (-rx, bot)], C["blue"], 3)
    c.line([(rx, top), (rx, bot)], C["blue"], 3)
    filled = base + sum(a)
    for i, vals in enumerate(_DB_ROWS):
        vis = clamp(filled - i)
        if vis <= 0:
            continue
        rest = bot - 16 - i * 16
        y = rest if i < base else lerp(top + 4, rest, e_out(vis))
        with c.at(0, y), c.fade(vis):
            for j, v in enumerate(vals):
                col = (mix(C["panel2"], C["accent"], .35 + .65 * v) if v > 0
                       else mix(C["panel2"], C["blue"], .35 - .65 * v))
                c.rect(-43 + j * 14.5, -4.5, 12.5, 9, 2, fill=col)
            if i >= base:
                c.rect(-48, -8, 96, 16, 4, stroke=C["accent"], sw=2.5, a=bump(vis))
    c.ellipse(0, top, rx, ry, fill=C["panel2"], stroke=C["blue"], sw=3)


_G_PTS = [(-44, -22), (-12, -42), (22, -26), (46, 6), (12, 10), (-28, 16), (-4, 40), (34, 38)]
_G_EDGES = [(0, 1), (1, 2), (2, 3), (1, 4), (4, 3), (0, 5), (5, 4), (5, 6), (6, 4), (6, 7), (7, 3), (2, 4)]
_G_HOP = [0, 5, 4, 3]


def _i_graph(c, a, d, node):
    c.circle(0, 0, 66, fill=C["panel"], stroke=C["edge"], sw=2)
    k = (a[-1] if a else 0) * len(_G_HOP)
    for i, j in _G_EDGES:
        hi = _G_HOP.index(i) if i in _G_HOP else -1
        hj = _G_HOP.index(j) if j in _G_HOP else -1
        on = clamp(k - max(hi, hj)) if (hi >= 0 and hj == hi + 1) or (hj >= 0 and hi == hj + 1) else 0
        c.line([_G_PTS[i], _G_PTS[j]], mix(C["edge"], C["accent"], on), 2.5 + 2 * on)
    for i, (x, y) in enumerate(_G_PTS):
        h = _G_HOP.index(i) if i in _G_HOP else -1
        c.circle(x, y, 7.5, fill=mix(C["ink"], C["accent"], clamp(k - h)) if h >= 0 else C["ink"],
                 stroke=C["blue"], sw=2.5)


def _i_merge(c, a, d, node):
    c.poly([(-64, -50), (64, -50), (18, 8), (18, 50), (-18, 50), (-18, 8)], fill=C["panel"],
           stroke=C["accent"], sw=3)
    c.rect(-46, -38, 36, 10, 5, fill=C["blue"], a=.3 + .7 * (a[0] if a else 0))
    c.rect(10, -38, 36, 10, 5, fill=C["accent"], a=.3 + .7 * (a[1] if len(a) > 1 else 0))
    h = 40 * (sum(a) / max(2, len(a)) if a else 0)
    c.rect(-14, 46 - h, 28, h, 3, fill=C["accent"])


def _i_list(c, a, d, node):
    w, h = 236, 132
    x0, y0 = -w / 2, -h / 2
    c.rect(x0, y0, w, h, 14, fill=C["panel"], stroke=C["blue"], sw=3)
    live = [i for i, p in enumerate(a) if p > 0]
    k = live[-1] if live else -1
    p = a[k] if k >= 0 else 0
    carry = node["incoming"][k] if k >= 0 else None
    items = (carry or {}).get("items") or []
    for i in range(3):
        y = y0 + 26 + i * 40
        on = clamp(p * 3 - i * .6)
        item = items[i] if i < len(items) else None
        shown = bool(item) and on > 0
        with c.fade(.35 + .65 * on):
            c.circle(x0 + 24, y, 12, fill=C["panel2"], stroke=C["accent"] if shown else C["blue"], sw=2)
            c.text(x0 + 24, y, str(i + 1), 14, weight=700)
            if shown:
                c.text(x0 + 44, y, ellipsize(c, item, 15, w - 54, 500), 15, weight=500, anchor="lm")
            else:
                c.rect(x0 + 46, y - 5, 120 - i * 22, 10, 5, fill=C["dim"])


def _i_user(c, a, d, node):
    c.circle(0, -24, 22, fill=C["panel2"], stroke=C["blue"], sw=3)
    body = Path.cubic((-44, 46), (-44, 4), (44, 4), (44, 46)).pts
    c.poly(body, fill=C["panel2"], stroke=C["blue"], sw=3)


def _i_server(c, a, d, node):
    c.rect(-50, -58, 100, 116, 10, fill=C["panel"], stroke=C["blue"], sw=3)
    p = peak(a, bump)
    for i, y in enumerate((-34, -2, 30)):
        c.rect(-36, y - 7, 46, 14, 4, fill=C["panel2"])
        c.circle(28, y, 5.5, fill=mix(C["dim"], C["good"], clamp(p * 3 - i)))


def _i_browser(c, a, d, node):
    c.rect(-68, -48, 136, 96, 10, fill=C["panel"], stroke=C["blue"], sw=3)
    c.line([(-68, -26), (68, -26)], C["blue"], 2.5)
    for x in (-54, -42, -30):
        c.circle(x, -37, 4, fill=C["muted"])
    for x, y, w in ((-50, -8, 70), (-50, 8, 90), (-50, 24, 56)):
        c.rect(x, y, w, 7, 3.5, fill=C["dim"])


def _i_cache(c, a, d, node):
    c.rect(-50, -50, 100, 100, 16, fill=C["panel"], stroke=C["blue"], sw=3)
    c.poly([(8, -34), (-18, 6), (2, 6), (-8, 36), (20, -6), (0, -6)], fill=C["accent"])


def _i_lock(c, a, d, node):
    arc = [(-26, -8), (-26, -28)] + [(-26 * math.cos(u), -28 - 26 * math.sin(u))
                                     for u in (i * math.pi / 16 for i in range(17))]
    c.line(arc + [(26, -8)], C["blue"], 8)
    c.rect(-42, -10, 84, 66, 10, fill=C["panel"], stroke=C["blue"], sw=3)
    c.circle(0, 16, 8, fill=C["accent"])
    c.rect(-3, 18, 6, 18, 3, fill=C["accent"])


_CLOUD = [(-30, 6, 26), (2, -6, 34), (30, 8, 26)]


def _cloud_outline():
    top = []
    for i in range(57):
        x = -56 + i * 2
        ys = [cy - math.sqrt(r * r - (x - cx) ** 2) for cx, cy, r in _CLOUD if abs(x - cx) <= r]
        if ys and min(ys) < 30:
            top.append((x, min(ys)))
    return top + [(top[-1][0], 30), (top[0][0], 30)]


_CLOUD_PTS = _cloud_outline()


def _i_cloud(c, a, d, node):
    c.poly(_CLOUD_PTS, fill=C["panel"], stroke=C["blue"], sw=3)


def _i_api(c, a, d, node):
    c.rect(-58, -44, 116, 88, 16, fill=C["panel"], stroke=C["blue"], sw=3)
    c.text(0, 0, "</>", 36, C["accent"], weight=700)


ICONS = {"query": (_i_query, 144, 68), "document": (_i_document, 88, 116),
         "index": (_i_index, 128, 108), "encoder": (_i_encoder, 140, 108),
         "model": (_i_model, 120, 120), "database": (_i_database, 120, 130),
         "graph": (_i_graph, 132, 132), "merge": (_i_merge, 128, 100),
         "list": (_i_list, 236, 132), "user": (_i_user, 92, 110),
         "server": (_i_server, 100, 116), "browser": (_i_browser, 136, 96),
         "cache": (_i_cache, 100, 100), "lock": (_i_lock, 84, 120),
         "cloud": (_i_cloud, 120, 84), "api": (_i_api, 116, 88)}
CARRY_KINDS = ("text", "vector", "list", "doc", "packet")


# ---------------------------------------------------------------------------
# carries: the real thing that travels an arrow
# ---------------------------------------------------------------------------
def carry_spec(c, carry, key):
    """(width, height, draw) for a carry; draw paints it centred on (0,0)."""
    kind = (carry or {}).get("kind")
    if kind == "text":
        label = ellipsize(c, carry.get("text", ""), 23, 300, 600)
        w, h = c.tw(label, 23, 600) + 36, 46

        def draw():
            c.rect(-w / 2, -h / 2, w, h, h / 2, fill=C["chip"], stroke=C["accent"], sw=2.5)
            c.text(0, 0, label, 23, weight=600)
        return w, h, draw
    if kind == "vector":
        r = seeded(carry.get("text") or key)
        vals = [round((r() * 1.9 - .95) * 100) / 100 for _ in range(3)]
        cw, ch, gap = 64, 40, 6
        inner = 3 * cw + 2 * gap + 30
        w, h = inner + 36, 54

        def draw():
            c.rect(-w / 2, -h / 2, w, h, 10, fill=C["vec"], stroke=C["blue"], sw=2)
            c.text(-w / 2 + 12, -1, "[", 30, C["muted"])
            c.text(w / 2 - 12, -1, "]", 30, C["muted"])
            for i, v in enumerate(vals):
                x = -inner / 2 + i * (cw + gap)
                fill = (mix(C["chip"], C["accent"], .25 + .6 * v) if v >= 0
                        else mix(C["chip"], C["blue"], .25 - .6 * v))
                c.rect(x, -ch / 2, cw, ch, 6, fill=fill)
                c.text(x + cw / 2, 0, ("−" if v < 0 else "") + f"{abs(v):.2f}", 19, weight=700)
            c.text(inner / 2 - 12, 0, "…", 26, C["muted"])
        return w, h, draw
    if kind == "list":
        items = [str(x) for x in (carry.get("items") or [])][:3]
        w, row_h = 220, 28
        h = 18 + len(items) * row_h

        def draw():
            c.rect(-w / 2, -h / 2, w, h, 10, fill=C["chip"], stroke=C["accent"], sw=2)
            for i, it in enumerate(items):
                y = -h / 2 + 9 + row_h * i + row_h / 2
                c.text(-w / 2 + 18, y, f"{i + 1}.", 17, C["accent"], weight=700)
                c.text(-w / 2 + 34, y, ellipsize(c, it, 17, w - 50, 500), 17, weight=500, anchor="lm")
        return w, h, draw
    if kind == "doc":
        def draw():
            c.poly([(-17, -22), (7, -22), (17, -12), (17, 22), (-17, 22)], fill=C["panel2"],
                   stroke=C["accent"], sw=2.5)
            for y in (-8, 0, 8):
                c.rect(-10, y, 20, 3.5, 1.75, fill=C["muted"])
        return 34, 44, draw

    def draw():
        c.rect(-13, -13, 26, 26, 7, fill=C["accent"])
    return 26, 26, draw


# ---------------------------------------------------------------------------
# layered DAG layout (loops allowed: back edges become arcs over the top)
# ---------------------------------------------------------------------------
def layout_dag(nodes, edges, cy, lane_gap):
    ids = [n["id"] for n in nodes]
    out = {i: [] for i in ids}
    for i, e in enumerate(edges):
        out[e["from"]].append((e["to"], i))
    back, state = set(), {}

    def dfs(u):
        state[u] = 1
        for to, i in out[u]:
            if state.get(to) == 1:
                back.add(i)
            elif not state.get(to):
                dfs(to)
        state[u] = 2
    for u in ids:
        if not state.get(u):
            dfs(u)
    parents = {i: [] for i in ids}
    for i, e in enumerate(edges):
        if i not in back:
            parents[e["to"]].append(e["from"])
    level: dict[str, int] = {}

    def visit(u):
        if u in level:
            return level[u]
        level[u] = 0
        level[u] = max((visit(p) + 1 for p in parents[u]), default=0)
        return level[u]
    for u in ids:
        visit(u)
    max_l = max(level.values(), default=0)
    spacing = min(520, 1300 / max_l) if max_l else 0
    x0 = 800 - spacing * max_l / 2
    pos: dict[str, list] = {}
    for lv in range(max_l + 1):
        in_l = [u for u in ids if level[u] == lv]
        for u in in_l:
            ps = parents[u]
            pos[u] = [x0 + lv * spacing, sum(pos[p][1] for p in ps) / len(ps) if ps else cy]
        if not in_l:
            continue
        want = sum(pos[u][1] for u in in_l) / len(in_l)
        in_l.sort(key=lambda u: (pos[u][1], ids.index(u)))
        for i in range(1, len(in_l)):
            if pos[in_l[i]][1] - pos[in_l[i - 1]][1] < lane_gap:
                pos[in_l[i]][1] = pos[in_l[i - 1]][1] + lane_gap
        got = sum(pos[u][1] for u in in_l) / len(in_l)
        for u in in_l:
            pos[u][1] = clamp(pos[u][1] + (want - got), 200, 560)
    return pos, level, max_l, spacing, back


# ---------------------------------------------------------------------------
# archetypes: build(c, slots, t, dur) draws the frame at local time t
# ---------------------------------------------------------------------------
def title(c, s, t, dur):
    lines, size = fit(c, s.get("title", ""), 1360, 100, 58, 2, 700)
    lines = lines or [""]
    lh = size * 1.15
    words = []
    for li, line in enumerate(lines):
        space, total = c.tw(" ", size, 700), c.tw(line, size, 700)
        x, y = 800 - total / 2, 360 - (len(lines) - 1) * lh / 2 + li * lh
        for p in line.split(" "):
            pw = c.tw(p, size, 700)
            words.append((p, x + pw / 2, y))
            x += pw + space
    for i, (w, x, y) in enumerate(words):
        p = e_out(seg(t, .15 + i * .1, .65 + i * .1))
        c.text(x, y + 36 * (1 - p), w, size, a=p, weight=700)
    last_y = 360 + (len(lines) - 1) * lh / 2
    ul_y = last_y + size * .62
    ul_w = min(560, c.tw(lines[-1], size, 700) * .62)
    u = e_inout(seg(t, .8, 1.35))
    half = ul_w / 2 * u
    if u > 0:
        c.line([(800 - half, ul_y), (800 + half, ul_y)], C["accent"], 6, cap=True)
        c.circle(800 - half, ul_y, 8, fill=C["accent"], a=u)
        c.circle(800 + half, ul_y, 8, fill=C["accent"], a=u)
    sub, ss = fit(c, s.get("subtitle", ""), 1200, 36, 26, 2)
    if sub:
        multiline(c, sub, 800, ul_y + 64 + (len(sub) - 1) * ss * .61, ss, C["muted"],
                  a=e_out(seg(t, 1.1, 1.7)))


def _norm(w):
    return "".join(ch for ch in w.lower() if ch.isalnum())


def keyword_match(c, s, t, dur):
    q_words = str(s.get("query", "")).split()[:4]
    d_words = str(s.get("document", "")).split()[:10]
    if not q_words or not d_words:
        return
    hits = [[i for i, dw in enumerate(d_words) if _norm(dw) and _norm(dw) == _norm(q)] for q in q_words]
    # search bar holding the query words
    qs, q_pad, q_gap = 42, 26, 16
    q_w = [c.tw(w, qs, 600) + q_pad * 2 for w in q_words]
    bar_h = 92
    bar_w = sum(q_w) + q_gap * (len(q_words) - 1) + 120
    bar_x, bar_y = 800 - bar_w / 2, 96
    bar_cy = bar_y + bar_h / 2
    q_pos, cx = [], bar_x + 108
    for w in q_w:
        q_pos.append(cx + w / 2)
        cx += w + q_gap
    # document card: its words laid out as chips
    ds, d_pad, d_gap, max_line = 40, 15, 12, 1000
    lines, lw = [[]], 0.0
    for i, w in enumerate(d_words):
        cw = c.tw(w, ds, 500) + d_pad * 2
        if lw and lw + d_gap + cw > max_line:
            lines.append([])
            lw = 0.0
        lines[-1].append((w, i, cw))
        lw += (d_gap if lw else 0) + cw
    content_w = max(sum(cw for _, _, cw in ln) + d_gap * (len(ln) - 1) for ln in lines)
    card_w = max(560, content_w + 120)
    card_h = 84 + len(lines) * 76 + 30
    card_x, card_y = 800 - card_w / 2 - 120, 360
    chips = {}
    for li, ln in enumerate(lines):
        x, y = card_x + 60, card_y + 96 + li * 76
        for w, i, cw in ln:
            chips[i] = (x, y, cw, w)
            x += cw + d_gap
    ghost_y = card_y - 50
    n = len(q_words)
    ghost_x = [card_x + card_w / 2 + (i - (n - 1) / 2) * min(280, card_w / n) for i in range(n)]
    score = s.get("score") if isinstance(s.get("score"), (int, float)) else None
    found = sum(1 for h in hits if h)
    check_a = 1.15
    check_b = max(check_a + 1.2 * n, dur - 1.7)
    per = (check_b - check_a) / max(1, n)
    s_a = check_b + .1

    bp = e_out(seg(t, .15, .6))
    with c.at(0, -24 * (1 - bp)), c.fade(bp):
        c.rect(bar_x, bar_y, bar_w, bar_h, bar_h / 2, fill=C["panel"], stroke=C["edge"], sw=3)
        c.circle(bar_x + 54, bar_cy - 4, 19, stroke=C["accent"], sw=7)
        c.line([(bar_x + 68, bar_cy + 10), (bar_x + 83, bar_cy + 25)], C["accent"], 8, cap=True)
        for w, xq, wq in zip(q_words, q_pos, q_w):
            c.rect(xq - wq / 2, bar_cy - 31, wq, 62, 31, fill=C["chip"], stroke=C["accent"], sw=2.5)
            c.text(xq, bar_cy, w, qs, weight=600)

    # per-word state at time t
    chip_state = {i: None for i in chips}       # None | ("near", amount) | "hit"
    beam_x, ghosts, links = None, [], []
    for i, w in enumerate(q_words):
        ws = check_a + i * per
        drop = e_inout(seg(t, ws, ws + per * .3))
        scan = seg(t, ws + per * .3, ws + per * .8)
        verdict = seg(t, ws + per * .8, ws + per * .95)
        gx, gy = lerp(q_pos[i], ghost_x[i], drop), lerp(bar_cy, ghost_y, drop)
        if 0 < scan < 1:
            beam_x = lerp(card_x - 110, card_x + card_w, scan)
            for j, (x, y, cw, _) in chips.items():
                near = 1 - clamp(abs(x + cw / 2 - (beam_x + 55)) / 80)
                if near > 0 and j not in hits[i] and chip_state[j] != "hit":
                    chip_state[j] = ("near", near)
        matched = False
        for j in hits[i]:
            x, y, cw, _ = chips[j]
            at = (x + cw / 2 - card_x + 55) / (card_w + 110)
            if scan >= at:
                chip_state[j] = "hit"
                links.append((gx, gy + 29, x + cw / 2, y - 31))
                matched = True
        if t >= ws:
            alpha = 1.0 if hits[i] else lerp(1, .55, verdict)
            stroke = (C["good"] if matched else
                      mix(C["accent"], C["bad"], verdict) if not hits[i] else C["accent"])
            ghosts.append((w, q_w[i], gx, gy, lerp(1.05, .9, drop), alpha, stroke,
                           verdict if not hits[i] else 0))

    cp = e_out(seg(t, .45, .95))
    with c.at(0, 30 * (1 - cp)), c.fade(cp):
        fold = card_x + card_w - 44
        c.poly([(card_x, card_y), (fold, card_y), (card_x + card_w, card_y + 44),
                (card_x + card_w, card_y + card_h), (card_x, card_y + card_h)],
               fill=C["panel"], stroke=C["blue"], sw=3)
        c.line([(fold, card_y), (fold, card_y + 44), (card_x + card_w, card_y + 44)], C["blue"], 3)
        c.rect(card_x + 40, card_y + 28, 150, 10, 5, fill=C["dim"])
        if beam_x is not None:
            bx0, bx1 = max(card_x, beam_x), min(card_x + card_w, beam_x + 110)
            c.rect(bx0, card_y, bx1 - bx0, card_h, fill=C["accent"], a=.22)
        for j, (x, y, cw, w) in chips.items():
            st = chip_state[j]
            if st == "hit":
                c.rect(x, y - 31, cw, 62, 12, fill=C["good"], a=.22)
                c.rect(x, y - 31, cw, 62, 12, stroke=C["good"], sw=2.5)
            elif st:
                c.rect(x, y - 31, cw, 62, 12, stroke=mix(C["panel"], C["accent"], st[1] * .8), sw=2.5)
            c.text(x + cw / 2, y, w, ds, weight=500)

    for x1, y1, x2, y2 in links:
        c.dashed([(x1, y1), (x2, y2)], C["good"], 3, dash=6, gap=6)
    for w, wq, gx, gy, sc, alpha, stroke, miss in ghosts:
        with c.at(gx, gy, sc), c.fade(alpha):
            c.rect(-wq / 2, -29, wq, 58, 29, fill=C["chip"], stroke=stroke, sw=2.5)
            c.text(0, 0, w, 36, weight=600)
            if miss > 0:
                with c.fade(miss), c.at(wq / 2 - 6, -26):
                    c.circle(0, 0, 19, fill=C["bad"])
                    c.line([(-7, -7), (7, 7)], C["badink"], 4.5, cap=True)
                    c.line([(-7, 7), (7, -7)], C["badink"], 4.5, cap=True)

    sp = seg(t, s_a, s_a + .5)
    zero = score == 0 or (score is None and not found)
    shake_p = seg(t, s_a + .4, s_a + .8)
    shake = math.sin(shake_p * math.pi * 6) * 6 * (1 - shake_p) if zero else 0
    bx, by = card_x + card_w + 170, card_y + card_h / 2
    if sp > 0:
        with c.at(bx + shake, by, lerp(.7, 1, e_back(sp))), c.fade(e_out(sp)):
            c.circle(0, 0, 96, fill=C["panel"])
            sweep = 360 * e_out(sp)
            arc = [(96 * math.cos(math.radians(-90 + u)), 96 * math.sin(math.radians(-90 + u)))
                   for u in [i * sweep / 48 for i in range(49)]]
            c.line(arc, C["bad"] if zero else C["good"], 6)
            val = found if score is None else score * e_out(seg(t, s_a + .2, s_a + .9))
            num = str(round(val)) if score is None or float(score).is_integer() else f"{val:.1f}"
            c.text(0, 0, num, 84, weight=700)
            c.text(0, 134, "Words matched" if score is None else (s.get("score_label") or "Score"),
                   30, C["muted"])
    c.text(card_x + card_w / 2, card_y + card_h + 56,
           f"{found} of {n} query words found in the document", 30, C["muted"],
           a=e_out(seg(t, s_a + .2, s_a + .7)))


def flow(c, s, t, dur):
    nodes = [n for n in (s.get("nodes") or []) if isinstance(n, dict) and n.get("id")][:7]
    ids = {n["id"] for n in nodes}
    edges = [e for e in (s.get("edges") or []) if isinstance(e, dict)
             and e.get("from") in ids and e.get("to") in ids and e["from"] != e["to"]]
    if not nodes:
        return
    sp0 = layout_dag(nodes, edges, 360, 300)
    S = clamp(((sp0[3] if sp0[2] else 520) - 80) / 170, 1, 1.5)
    pos, level, max_l, spacing, back = layout_dag(nodes, edges, 360, 100 + 140 * S)
    sp = spacing or 520
    N = {}
    for n in nodes:
        x, y = pos[n["id"]]
        node = {"n": n, "x": x, "y": y, "level": level[n["id"]], "sc": 1.0,
                "arr": [], "dep": [], "incoming": []}
        icon = ICONS.get(n.get("icon") or "")
        if icon:
            fn, w0, h0 = icon
            sc = min(S, (sp - 60) / w0, 1.6)
            node.update(icon=fn, sc=sc, w=w0 * sc, h=h0 * sc)
            node["label"], node["lsize"] = fit(c, n.get("label", ""), min(sp - 30, 320), 31, 22, 2, 500)
        else:      # text box, measured so it never leaves its column
            lines, size = fit(c, n.get("label", ""), min(sp - 60, 340) - 32, 34, 22, 2, 500)
            node.update(icon=None, label=lines, lsize=size,
                        w=max(c.tw(ln, size, 500) for ln in lines) + 40 if lines else 120,
                        h=len(lines) * size * 1.22 + 30)
        N[n["id"]] = node
    intro = min(1.7, .3 + .28 * (max_l + 1))
    groups = sorted({N[e["from"]]["level"] for e in edges})
    win = max(.6, min(2.3, (dur - .7 - intro) / max(1, len(groups))))
    ED = []
    for ei, e in enumerate(edges):
        a, b = N[e["from"]], N[e["to"]]
        is_back = ei in back or b["x"] <= a["x"]
        if is_back:
            sx, sy = a["x"], a["y"] - a["h"] / 2 - 8
            tx, ty = b["x"], b["y"] - b["h"] / 2 - 14
            lift = min(sy, ty) - 120
            path = Path.cubic((sx, sy), (sx, lift), (tx, lift), (tx, ty))
        else:
            sx, sy = a["x"] + a["w"] / 2 + 12, a["y"]
            tx, ty = b["x"] - b["w"] / 2 - 16, b["y"]
            dx = max(50, (tx - sx) * .5)
            path = Path.cubic((sx, sy), (sx + dx, sy), (tx - dx, ty), (tx, ty))
        p1 = path.point(path.length - 3)
        ang = math.atan2(ty - p1[1], tx - p1[0])
        gi = groups.index(a["level"])
        ws = intro + gi * win
        t_end, we = ws + win * .68, ws + win
        b["arr"].append((t_end, we, e.get("carry")))
        a["dep"].append((ws, ws + win * .3))
        draw_a = .3 + a["level"] * .28
        cw, ch, cdraw = carry_spec(c, e.get("carry"), e["from"] + e["to"])
        cs = clamp(math.hypot(tx - sx, ty - sy) * 1.1 / cw, .85, 1.35)
        ED.append({"e": e, "a": a, "b": b, "path": path, "tx": tx, "ty": ty, "ang": ang,
                   "ws": ws, "t_end": t_end, "we": we, "draw_a": draw_a,
                   "draw_b": draw_a + .45, "cdraw": cdraw, "cs": cs})
    for nd in N.values():
        nd["arr"].sort(key=lambda x: x[0])
        nd["incoming"] = [x[2] for x in nd["arr"]]

    # 1) edges, arrowheads and edge labels
    for ed in ED:
        dp = e_inout(seg(t, ed["draw_a"], ed["draw_b"]))
        active = ed["ws"] <= t <= ed["we"]
        col = mix(C["edge"], C["accent"], .9) if active else C["edge"]
        if dp > 0:
            c.line(ed["path"].slice(0, ed["path"].length * dp), col, 3.5, cap=True)
        if dp > .96:
            ca, sa = math.cos(ed["ang"]), math.sin(ed["ang"])
            head = [(ed["tx"] + px * ca - py * sa, ed["ty"] + px * sa + py * ca)
                    for px, py in ((2, 0), (-12, -8), (-12, 8))]
            c.poly(head, fill=col)
        label = ed["e"].get("label")
        if label:
            la = e_out(seg(t, ed["draw_b"], ed["draw_b"] + .4))
            mx, my = ed["path"].point(ed["path"].length / 2)
            lt = ellipsize(c, label, 21, 230, 500)
            lw = c.tw(lt, 21, 500) + 26
            c.rect(mx - lw / 2, my - 60, lw, 34, 17, fill=C["ink"], stroke=C["edge"], sw=1.5, a=la)
            c.text(mx, my - 43, lt, 21, C["muted"], a=la, weight=500)

    # 2) nodes: pop in by level, glow on arrival/departure, icons react
    for nd in N.values():
        ap = seg(t, .15 + nd["level"] * .28, .55 + nd["level"] * .28)
        if ap <= 0:
            continue
        arr = [seg(t, a0, a1) for a0, a1, _ in nd["arr"]]
        dep = [seg(t, d0, d1) for d0, d1 in nd["dep"]]
        halo = .45 * max([bump(p) for p in arr] + [.6 * bump(p) for p in dep] + [0])
        with c.at(nd["x"], nd["y"], lerp(.6, 1, e_back(ap))), c.fade(e_out(ap)):
            c.glow(0, 0, max(nd["w"], nd["h"]) / 2 + 22, C["accent"], halo)
            if nd["icon"]:
                with c.at(0, 0, nd["sc"]):
                    nd["icon"](c, arr, dep, nd)
                lh = nd["lsize"] * 1.22
                multiline(c, nd["label"], 0, nd["h"] / 2 + 18 + len(nd["label"]) * lh / 2,
                          nd["lsize"], weight=500)
            else:
                c.rect(-nd["w"] / 2, -nd["h"] / 2, nd["w"], nd["h"], 14, fill=C["panel"],
                       stroke=C["edge"], sw=2.5)
                multiline(c, nd["label"], 0, 0, nd["lsize"], weight=500)

    # 3) carries ride the arrows, then shrink into their target
    for ed in ED:
        if not (ed["ws"] <= t <= ed["we"]):
            continue
        tp = e_inout(seg(t, ed["ws"], ed["t_end"]))
        ab = e_inout(seg(t, ed["t_end"], ed["we"]))
        em = e_out(seg(t, ed["ws"], ed["ws"] + .28))
        px, py = ed["path"].point(ed["path"].length * tp)
        a, b = ed["a"], ed["b"]
        drop_y = -48 * b["sc"] if (b["n"].get("icon") == "database") else 0
        ex, ey = lerp(a["x"], px, em), lerp(a["y"], py, em)
        x, y = lerp(ex, b["x"], ab), lerp(ey, b["y"] + drop_y, ab)
        with c.at(x, y, ed["cs"] * lerp(.45, 1, em) * lerp(1, .22, ab)), c.fade(em * (1 - ab)):
            ed["cdraw"]()


def vector_space(c, s, t, dur):
    ox, oy, sx, sy = 800, 385, 540, 265
    vs = [v for v in (s.get("vectors") or []) if isinstance(v, dict)][:5]
    if not vs:
        return
    q = s.get("query") or None
    nb = set(s.get("neighbors") or [])
    rnd = seeded("space")
    dots_a = e_out(seg(t, .05, .6))
    for _ in range(90):
        ang, r = rnd() * math.pi * 2, math.sqrt(rnd())
        c.circle(ox + math.cos(ang) * r * sx * 1.02, oy + math.sin(ang) * r * sy * 1.15, 2.4,
                 fill=C["dim"], a=.55 * dots_a)
    c.line([(ox - sx - 40, oy), (ox + sx + 40, oy)], C["edge"], 2)
    c.line([(ox, oy - sy - 50), (ox, oy + sy + 30)], C["edge"], 2)
    A = []
    for v in vs:
        tip = (ox + clamp(float(v.get("x") or 0), -1, 1) * sx,
               oy - clamp(float(v.get("y") or 0), -1, 1) * sy)
        label = str(v.get("label", ""))
        role = ("query" if label == q else "near" if label in nb
                else "far" if q else ("query" if v.get("highlight") else "near"))
        col = C["accent"] if role == "query" else C["blue"] if role == "near" else C["far"]
        lt = ellipsize(c, label, 33, 400, 600)
        lw = c.tw(lt, 33, 600) + 20
        right = tip[0] >= ox
        lx = tip[0] + 14 + lw / 2 if right else tip[0] - 14 - lw / 2
        if lx + lw / 2 > 1580:
            lx = tip[0] - 14 - lw / 2
        if lx - lw / 2 < 20:
            lx = tip[0] + 14 + lw / 2
        A.append({"tip": tip, "role": role, "col": col, "lt": lt, "lw": lw, "lh": 44,
                  "lx": lx, "ly": tip[1] - 4})
    for _ in range(60):                 # push overlapping labels apart vertically
        for i in range(len(A)):
            for j in range(i + 1, len(A)):
                a, b = A[i], A[j]
                ox_ = (a["lw"] + b["lw"]) / 2 + 8 - abs(a["lx"] - b["lx"])
                oy_ = (a["lh"] + b["lh"]) / 2 + 6 - abs(a["ly"] - b["ly"])
                if ox_ > 0 and oy_ > 0:
                    dd = oy_ / 2 + .5
                    up, dn = (a, b) if a["ly"] <= b["ly"] else (b, a)
                    up["ly"] -= dd
                    dn["ly"] += dd
    for a in A:
        a["ly"] = clamp(a["ly"], 60, 690)
    n = len(A)
    grown = .35 + n * .32 + .5
    qa = next((a for a in A if a["role"] == "query"), None)
    na = next((a for a in A if a["role"] == "near"), None) if qa else None
    hp = e_out(seg(t, grown + .9, grown + 1.5)) if qa else 0
    if qa:
        for k in range(3):
            p = seg(t, grown + k * .38, grown + k * .38 + 1.15)
            if 0 < p < 1:
                c.circle(*qa["tip"], 10 + 190 * e_out(p), stroke=C["accent"], sw=3, a=.75 * (1 - p))
    for i, a in enumerate(A):
        dim = lerp(1, .3, hp) if a["role"] == "far" else 1
        with c.fade(dim):
            p = e_out(seg(t, .35 + i * .32, .95 + i * .32))
            if p > .02:
                x, y = lerp(ox, a["tip"][0], p), lerp(oy, a["tip"][1], p)
                width = 6 + 4 * hp if a["role"] == "near" else 6
                c.line([(ox, oy), (x, y)], a["col"], width, cap=True)
                ang = math.atan2(a["tip"][1] - oy, a["tip"][0] - ox)
                ca, sa = math.cos(ang), math.sin(ang)
                c.poly([(x + px * ca - py * sa, y + px * sa + py * ca)
                        for px, py in ((5, 0), (-17, -11), (-17, 11))], fill=a["col"])
            lp = e_out(seg(t, .75 + i * .32, 1.15 + i * .32))
            if abs(a["ly"] - a["tip"][1]) > 14:
                end_x = a["lx"] - (a["lw"] / 2 if a["lx"] > a["tip"][0] else -a["lw"] / 2)
                c.line([a["tip"], (end_x, a["ly"])], a["col"], 1.5, a=lp * .6)
            c.text(a["lx"], a["ly"], a["lt"], 33, a["col"], a=lp, weight=600)
    if qa and na:
        tx, ty = lerp(qa["tip"][0], na["tip"][0], hp), lerp(qa["tip"][1], na["tip"][1], hp)
        if hp > 0:
            c.dashed([qa["tip"], (tx, ty)], C["blue"], 3, dash=8, gap=7)
        a1 = math.atan2(qa["tip"][1] - oy, qa["tip"][0] - ox)
        a2 = math.atan2(na["tip"][1] - oy, na["tip"][0] - ox)
        ap = e_inout(seg(t, grown + 1.3, grown + 1.9))
        if ap > 0:
            end = lerp(a1, a2, ap)
            arc = [(ox + 150 * math.cos(lerp(a1, end, u / 32)), oy + 150 * math.sin(lerp(a1, end, u / 32)))
                   for u in range(33)]
            c.line(arc, C["accent"], 4, cap=True)


def compare_two(c, s, t, dur):
    L, R = s.get("left") or {}, s.get("right") or {}
    rows = max(len(L.get("points") or []), len(R.get("points") or []), 1)
    pw, gap = 630, 130
    ph = 180 + rows * 80
    top = max(90, 380 - ph / 2)
    lx, rx = 800 - gap / 2 - pw, 800 + gap / 2
    for i in range(rows):
        a0 = .95 + i * .6
        c.rect(lx + 16, top + 168 + i * 80 - 33, 2 * pw + gap - 32, 66, 12, fill=C["accent"],
               a=.1 * bump(seg(t, a0, a0 + .9)))
    for side, x, col, sgn in ((L, lx, C["blue"], -1), (R, rx, C["accent"], 1)):
        p = e_out(seg(t, .15, .7))
        with c.at(90 * sgn * (1 - p), 0), c.fade(p):
            c.rect(x, top, pw, ph, 20, fill=C["panel"], stroke=C["edge"], sw=2.5)
            tl, ts = fit(c, side.get("title", ""), pw - 80, 52, 30, 1, 650)
            c.text(x + pw / 2, top + 66, tl[0] if tl else "", ts, col, weight=650)
            c.rect(x + pw / 2 - 44, top + 104, 88, 6, 3, fill=col)
            for i, pt in enumerate(side.get("points") or []):
                a0 = .95 + i * .6
                pp = e_out(seg(t, a0, a0 + .4))
                y = top + 168 + i * 80
                with c.at(-24 * sgn * (1 - pp), 0), c.fade(pp):
                    c.circle(x + 58, y, 11, fill=col)
                    pl, psz = fit(c, pt, pw - 130, 36, 24, 1, 500)
                    c.text(x + 88, y, pl[0] if pl else "", psz, weight=500, anchor="lm")
    vp = seg(t, .55, 1)
    if vp > 0:
        with c.at(800, top + ph / 2, e_back(vp)):
            c.circle(0, 0, 52, fill=C["accent"])
            c.text(0, 0, "vs", 36, C["ink"], weight=700)


def callout(c, s, t, dur):
    lines, size = fit(c, s.get("text", ""), 1300, 76, 44, 3, 650)
    if not lines:
        return
    lh = size * 1.22
    k = 0
    for li, line in enumerate(lines):
        y = 370 - (len(lines) - 1) * lh / 2 + li * lh
        space = c.tw(" ", size, 650)
        x = 800 - c.tw(line, size, 650) / 2
        for w in line.split(" "):
            ww = c.tw(w, size, 650)
            p = e_out(seg(t, .2 + k * .08, .6 + k * .08))
            c.text(x + ww / 2, y + 22 * (1 - p), w, size, a=p, weight=650)
            x += ww + space
            k += 1
    last_w = c.tw(lines[-1], size, 650)
    ly = 370 + (len(lines) - 1) * lh / 2 + size * .72
    sw = last_w * e_inout(seg(t, .3 + k * .08, .9 + k * .08))
    c.rect(800 - last_w / 2, ly, sw, 7, 3.5, fill=C["accent"])


SCENES = {"title": title, "keyword_match": keyword_match, "flow": flow,
          "vector_space": vector_space, "compare_two": compare_two, "callout": callout}
