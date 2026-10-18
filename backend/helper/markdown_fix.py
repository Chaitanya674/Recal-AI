"""Repairs for model-written markdown that travelled inside a JSON string.

LaTeX needs every backslash doubled in JSON. Models routinely double `\\frac` but not
the matrix row break `\\\\`, and an undoubled `\\f`, `\\b`, `\\t`, `\\r`, `\\n`
silently becomes a control character on decode (`\\frac` -> form feed + "rac").
repair_math restores them and puts one-line `$$...$$` on their own lines so they
render as display math. Fenced code blocks are never touched.

normalize_citations rewrites grouped citations `[a, b]` as `[a][b]` and drops ids
that are not among the page's source chunks (an ungrounded citation is worse than
none)."""
from __future__ import annotations

import re

_FENCE = re.compile(r"(```.*?```)", re.S)
# never legitimate in markdown: always a LaTeX command whose backslash JSON consumed
_HARD_CTRL = {"\x0c": "\\f", "\x08": "\\b", "\x0b": "\\v", "\x07": "\\a"}
# "\n" + a stem no English line starts with: \nabla \neq \notin \newline \nsubseteq...
_NEWLINE_CMD = re.compile(r"\n(?=(?:abla|eq|otin|ewline|subseteq|supseteq|vdash)(?![A-Za-z]))")
_TAB_CMD = re.compile(r"\t(?=[A-Za-z])")            # \text \times \theta \tau \to
_CR_CMD = re.compile(r"\r(?=[A-Za-z])")             # \right \rho \rangle
_ENV = re.compile(r"(\\begin\{(?:[pbvBV]?matrix|smallmatrix|cases|aligned|align\*?|array|"
                  r"gathered|split)\}.*?\\end\{[^}]+\})", re.S)
_ROW_BREAK = re.compile(r"(?<!\\)\\(?=\s|\[\d)")    # a lone "\" before a space or [6pt]
_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE = re.compile(r"(?<![\\$])\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)")


def _fix_tex(tex: str) -> str:
    tex = _CR_CMD.sub(r"\\r", _TAB_CMD.sub(r"\\t", tex))
    return _ENV.sub(lambda m: _ROW_BREAK.sub(r"\\\\", m.group(1)), tex)


def _repair_prose(part: str) -> str:
    for ch, rep in _HARD_CTRL.items():
        part = part.replace(ch, rep)
    part = _NEWLINE_CMD.sub(r"\\n", part)
    part = _DISPLAY.sub(lambda m: f"\n\n$$\n{_fix_tex(m.group(1)).strip()}\n$$\n\n", part)
    part = _INLINE.sub(lambda m: "$" + _fix_tex(m.group(1)) + "$", part)
    return re.sub(r"\n{3,}", "\n\n", part)


def repair_math(md: str) -> str:
    parts = _FENCE.split(md or "")
    return "".join(p if i % 2 else _repair_prose(p) for i, p in enumerate(parts))


_GROUP = re.compile(r"( ?)\[([^\[\]\n]{1,240})\](?!\()")
_ID_LIKE = re.compile(r"[0-9a-f]{12}|c\d{1,3}")


def normalize_citations(md: str, ids: set[str]) -> str:
    """[a, b] -> [a][b]; citation-shaped ids that are not real sources are dropped."""
    def repl(m):
        parts = [p for p in re.split(r"[\s,;]+", m.group(2).strip()) if p]
        if not parts or not all(_ID_LIKE.fullmatch(p) for p in parts):
            return m.group(0)                       # ordinary bracketed text: untouched
        known = [p for p in dict.fromkeys(parts) if p in ids]
        return (m.group(1) + "".join(f"[{p}]" for p in known)) if known else ""
    parts = _FENCE.split(md or "")
    return "".join(p if i % 2 else _GROUP.sub(repl, p) for i, p in enumerate(parts))
