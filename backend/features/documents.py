"""Uploaded documents (PPTX / DOCX / XLSX): parse into sections, then
  review(mode="message")      — did the deck/doc land its message? where are the gaps?
  review(mode="client_ready") — is it presentable to a client? cryptic headers, jargon,
                                walls of text, numbers with no units.

Heuristic findings are computed in code for every file; in real mode Gemini reviews the
sections with those findings as hints and may add more (via llm.route, so it is verified
and falls back to the heuristics if the model output is malformed). Uploads also feed the
knowledge graph, so Q&A and reels can cite them. Storage is in-memory per user."""
from __future__ import annotations

import io
import re
import uuid
from collections import Counter

from . import knowledge
from .llm import route
from ..database import fire, schema
from ..helper.config import settings

MAX_BYTES = 15 * 1024 * 1024
KINDS = {".pptx": "pptx", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx"}

_docs: dict[str, dict[str, dict]] = {}     # user -> doc_id -> doc


class DocError(Exception):
    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason, self.status = reason, status


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _parse_pptx(data: bytes) -> list[dict]:
    from pptx import Presentation
    secs = []
    for n, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        title, body = "", []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = shape.text_frame.text.strip()
            if not text:
                continue
            if shape == slide.shapes.title:
                title = text
            else:
                body += [ln.strip() for ln in text.splitlines() if ln.strip()]
        notes = (slide.notes_slide.notes_text_frame.text.strip()
                 if slide.has_notes_slide and slide.notes_slide.notes_text_frame else "")
        secs.append({"n": n, "kind": "slide", "title": title, "lines": body, "notes": notes})
    return secs


def _parse_docx(data: bytes) -> list[dict]:
    from docx import Document
    doc = Document(io.BytesIO(data))
    secs, cur = [], {"n": 1, "kind": "section", "title": "", "lines": [], "notes": ""}
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        if p.style.name.startswith("Heading"):
            if cur["lines"] or cur["title"]:
                secs.append(cur)
            cur = {"n": len(secs) + 1, "kind": "section", "title": text, "lines": [], "notes": ""}
        else:
            cur["lines"].append(text)
            if len(cur["lines"]) >= 40:          # headingless docs: cap section size
                secs.append(cur)
                cur = {"n": len(secs) + 1, "kind": "section", "title": "", "lines": [], "notes": ""}
    if cur["lines"] or cur["title"]:
        secs.append(cur)
    return secs


def _parse_xlsx(data: bytes) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    secs = []
    for n, ws in enumerate(wb.worksheets, 1):
        rows = ws.iter_rows(max_row=8, values_only=True)
        rows = [r for r in rows if any(v is not None for v in (r or []))]
        headers = [str(v).strip() if v is not None else "" for v in (rows[0] if rows else [])]
        sample = [["" if v is None else str(v)[:40] for v in r] for r in rows[1:6]]
        secs.append({"n": n, "kind": "sheet", "title": ws.title, "lines": [], "notes": "",
                     "headers": headers, "sample": sample,
                     "rows": ws.max_row or 0, "cols": ws.max_column or 0})
    wb.close()
    return secs


def parse_upload(user: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise DocError("file_too_large")
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    kind = KINDS.get(ext)
    if not kind:
        raise DocError("unsupported_type")
    try:
        sections = {"pptx": _parse_pptx, "docx": _parse_docx, "xlsx": _parse_xlsx}[kind](data)
    except DocError:
        raise
    except Exception:  # noqa: BLE001 — corrupt / password-protected file
        raise DocError("unreadable_file")
    if not sections:
        raise DocError("empty_document")
    doc = {"id": uuid.uuid4().hex[:12], "user": user, "name": filename, "kind": kind,
           "sections": sections}
    _docs.setdefault(user, {})[doc["id"]] = doc
    if fire.enabled():
        try:
            fire.set_doc({"name": filename, "kind": kind, "section_count": len(sections),
                          "meta": fire.meta()}, *schema.docs(user), doc["id"])
            for s in sections:    # Firestore forbids nested arrays: wrap sample rows
                stored = {**s, "sample": [{"c": r} for r in s.get("sample", [])],
                          "meta": fire.meta()}
                fire.set_doc(stored, *schema.doc_sections(user, doc["id"]), str(s["n"]))
            if fire.media_enabled():
                fire.blob_upload(f"users/{user}/uploads/{doc['id']}/original{ext}",
                                 data, "application/octet-stream")
        except Exception:  # noqa: BLE001 — in-session copy stays authoritative
            pass
    text = "\n\n".join(section_text(s) for s in sections)
    try:   # feed the knowledge graph so ask/reels can cite the doc
        knowledge.ingest_text(user, text, title=filename)
    except Exception:  # noqa: BLE001
        pass
    return doc


def get_doc(user: str, doc_id: str) -> dict:
    doc = _docs.get(user, {}).get(doc_id)
    if not doc and fire.enabled():     # lazy load-back after a restart
        try:
            head = fire.get_doc(*schema.docs(user), doc_id)
            if head:
                secs = []
                for _, d in fire.stream(*schema.doc_sections(user, doc_id)):
                    d.pop("meta", None)
                    d["sample"] = [r.get("c", []) for r in d.get("sample", [])]
                    secs.append(d)
                doc = {"id": doc_id, "user": user, "name": head["name"],
                       "kind": head["kind"], "sections": sorted(secs, key=lambda s: s["n"])}
                _docs.setdefault(user, {})[doc_id] = doc
        except Exception:  # noqa: BLE001
            doc = None
    if not doc:
        raise DocError("doc_not_found", 404)
    return doc


def list_docs(user: str) -> list[dict]:
    if fire.enabled():
        try:
            return [{"id": doc_id, "name": d["name"], "kind": d["kind"],
                     "sections": d.get("section_count", 0)}
                    for doc_id, d in fire.stream(*schema.docs(user))]
        except Exception:  # noqa: BLE001
            pass
    return [{"id": d["id"], "name": d["name"], "kind": d["kind"],
             "sections": len(d["sections"])} for d in _docs.get(user, {}).values()]


def section_text(s: dict) -> str:
    parts = [f"{s['kind']} {s['n']}: {s['title']}".strip()]
    if s.get("headers"):
        parts.append("columns: " + " | ".join(h or "(blank)" for h in s["headers"]))
        parts += ["row: " + " | ".join(r) for r in s.get("sample", [])[:2]]
    parts += s.get("lines", [])
    if s.get("notes"):
        parts.append("speaker notes: " + s["notes"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# heuristic findings (work with zero keys; hints for the model in real mode)
# ---------------------------------------------------------------------------
GENERIC = {"value", "values", "data", "amount", "amt", "number", "num", "total", "count",
           "type", "status", "flag", "id", "name", "desc", "description", "misc", "info",
           "metric", "qty", "item", "field", "category", "cat", "key", "code", "ref"}
ACTION_WORDS = ("next step", "recommendation", "we recommend", "action", "decision",
                "ask", "proposal", "timeline", "owner")
_ACRONYM = re.compile(r"\b[A-Z]{2,5}\d?\b")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*\.?\d*%?")


def _issue(where: str, severity: str, issue: str, fix: str) -> dict:
    return {"where": where, "severity": severity, "issue": issue, "fix": fix}


def _header_issues(s: dict) -> list[dict]:
    out, where = [], f"sheet {s['n']} “{s['title']}”"
    headers = [h for h in s.get("headers", [])]
    dupes = {h.lower() for h, c in Counter(h.lower() for h in headers if h).items() if c > 1}
    for i, h in enumerate(headers):
        col = f"{where}, column {i + 1}"
        base = h.lower().strip()
        if not base:
            out.append(_issue(col, "high", "Column has no header.",
                              "Name every column a client will see."))
            continue
        if base in dupes:
            out.append(_issue(col, "high", f"Duplicate header “{h}”.",
                              "Make each column name unique and specific."))
            dupes.discard(base)
        word = re.sub(r"[^a-z]", "", base)
        if base in GENERIC:
            out.append(_issue(col, "high", f"“{h}” is a generic term — a client can't tell what it measures.",
                              f"Spell it out, e.g. “{h} (what, unit, period)” → “Billed hours — Sep”."))
        elif len(base) <= 3 or (word and len(word) >= 3 and not set(word) & set("aeiou")):
            out.append(_issue(col, "medium", f"“{h}” reads as an internal abbreviation.",
                              "Expand it (e.g. “Qty” → “Quantity ordered”)."))
        elif "_" in base or re.search(r"[a-z][A-Z]", h):
            out.append(_issue(col, "low", f"“{h}” looks like a system field name.",
                              "Use plain words with spaces."))
    numericish = sum(1 for r in s.get("sample", []) for v in r if re.fullmatch(r"-?[\d,]+\.?\d*", v or ""))
    if numericish >= 3 and not any(u in " ".join(headers).lower()
                                   for u in ("%", "$", "₹", "€", "hrs", "hours", "days", "usd", "count", "#")):
        out.append(_issue(where, "medium", "Numeric columns but no units anywhere in the headers.",
                          "Put the unit in the header (e.g. “Effort (hrs)”, “Cost ($)”)."))
    return out


def _slide_issues(s: dict) -> list[dict]:
    out, where = [], f"slide {s['n']}" + (f" “{s['title']}”" if s["title"] else "")
    words = sum(len(ln.split()) for ln in s["lines"])
    if not s["title"]:
        out.append(_issue(where, "medium", "No title — the audience can't tell what the slide claims.",
                          "Give it a one-line takeaway title."))
    if words > 90:
        out.append(_issue(where, "high", f"Wall of text ({words} words).",
                          "Keep the one claim as the title; rebuild the body as the component "
                          "that fits its shape (chevron flow for a process, two-column table "
                          "for a comparison, bar chart for quantities); prose moves to "
                          "speaker notes or an appendix."))
    elif len(s["lines"]) > 8:
        out.append(_issue(where, "medium", f"{len(s['lines'])} bullets on one slide.",
                          "Split the slide or group bullets under 2–3 headings."))
    acronyms = {a for ln in [s["title"], *s["lines"]] for a in _ACRONYM.findall(ln or "")}
    if len(acronyms) >= 3:
        out.append(_issue(where, "low", f"Unexplained acronyms: {', '.join(sorted(acronyms)[:5])}.",
                          "Spell each out on first use."))
    return out


def _message_issues(doc: dict) -> list[dict]:
    out = []
    all_text = " ".join(section_text(s).lower() for s in doc["sections"])
    if not any(w in all_text for w in ACTION_WORDS):
        out.append(_issue("whole document", "high",
                          "No ask, recommendation or next step anywhere — the audience won't know what you want from them.",
                          "End with a “so what”: the decision you need, the owner and the date."))
    first = doc["sections"][0]
    if doc["kind"] == "pptx" and len(doc["sections"]) >= 4 and \
            not any(w in section_text(first).lower() for w in ("agenda", "summary", "overview", "context", "objective")):
        out.append(_issue("slide 1", "medium", "Opens without a summary or agenda.",
                          "Start with the one-slide story: situation → finding → ask."))
    bare_numbers = sum(1 for s in doc["sections"] for ln in s.get("lines", [])
                       if _NUMBER.search(ln) and len(ln.split()) <= 4)
    if bare_numbers >= 3:
        out.append(_issue("multiple sections", "medium",
                          f"{bare_numbers} numbers appear with no comparison or context.",
                          "Pair every number with a baseline, target or trend."))
    return out


def _merge_repeats(issues: list[dict]) -> list[dict]:
    """The same rule firing on 3+ sections becomes ONE issue, not an echo."""
    groups: dict[str, list[dict]] = {}
    for i in issues:
        groups.setdefault(i["fix"], []).append(i)
    out = []
    for same in groups.values():
        if len(same) < 3:
            out += same
            continue
        locs = [i["where"] for i in same]
        where = ", ".join(locs[:8]) + (f" +{len(locs) - 8} more" if len(locs) > 8 else "")
        out.append(_issue(where, same[0]["severity"],
                          f"Same problem in {len(same)} places — e.g. {same[0]['where']}: "
                          f"{same[0]['issue']}", same[0]["fix"]))
    return out


def heuristic_review(doc: dict, mode: str) -> dict:
    issues = []
    for s in doc["sections"]:
        if s["kind"] == "sheet":
            issues += _header_issues(s)
        elif mode == "client_ready":
            issues += _slide_issues(s)
    if mode == "message":
        issues += _message_issues(doc)
        if doc["kind"] == "pptx":
            for s in doc["sections"]:
                issues += [i for i in _slide_issues(s) if i["severity"] == "high"]
    issues = _merge_repeats(issues)
    first = doc["sections"][0]
    key_message = (first.get("title") or " ".join(first.get("lines", [])[:1]) or doc["name"])[:140]
    sev = Counter(i["severity"] for i in issues)
    headline = ("Reads clean — nothing major to fix." if not issues else
                f"{sev.get('high', 0)} blocking, {sev.get('medium', 0)} notable, "
                f"{sev.get('low', 0)} minor issue(s) before a client sees this.")
    return {"doc_id": doc["id"], "doc_name": doc["name"], "mode": mode,
            "headline": headline, "key_message": key_message,
            "issues": issues[:25],
            "stats": {"sections": len(doc["sections"]), "issues": len(issues)}}


# ---------------------------------------------------------------------------
# review = heuristics + (real mode) Gemini pass over the sections
# ---------------------------------------------------------------------------
def _valid_review(out: dict) -> tuple[bool, str]:
    if not isinstance(out, dict) or not isinstance(out.get("issues"), list):
        return False, "missing issues list"
    for i in out["issues"]:
        if not isinstance(i, dict) or not i.get("issue") or not i.get("fix"):
            return False, "issue entries need 'issue' and 'fix'"
        i.setdefault("where", "document")
        i.setdefault("severity", "medium")
    if not out.get("headline"):
        return False, "missing headline"
    return True, ""


def review(user: str, doc_id: str, mode: str = "message") -> tuple[dict, object]:
    if mode not in ("message", "client_ready"):
        raise DocError("bad_mode")
    doc = get_doc(user, doc_id)
    base = heuristic_review(doc, mode)
    content = "\n\n".join(section_text(s)[:1200] for s in doc["sections"][:30])
    focus = ("Does it land one clear message? Is there an explicit ask/next step? "
             "Do the sections build an argument, and where are the gaps a reader would trip on?"
             if mode == "message" else
             "Is this client-presentable? Flag cryptic or generic headers and labels, internal "
             "jargon, unexplained acronyms, numbers without units/context, walls of text, and "
             "content in the wrong form — prose that should be a diagram, table or chart, or "
             "a sheet whose rows don't make its point obvious.")
    from ..helper import prompt_loader
    prompt = prompt_loader.get("doc_review", doc=doc["name"], kind=doc["kind"], focus=focus,
                               hints="\n".join(f"- {i['where']}: {i['issue']}" for i in base["issues"]) or "none",
                               content=content)
    out, receipt = route("doc_review", prompt, verifier=_valid_review,
                         mock_output=base, domain=mode)
    if not receipt.verified:
        out = base
        receipt.verified, receipt.reason = True, "heuristic fallback (model review invalid)"
    out.update(doc_id=doc["id"], doc_name=doc["name"], mode=mode)
    out.setdefault("key_message", base["key_message"])
    out["issues"] = out["issues"][:25]
    out["stats"] = {"sections": len(doc["sections"]), "issues": len(out["issues"])}
    if not settings.MOCK:   # the model may miss mechanical finds; keep the union
        seen = {(i["where"], i["issue"]) for i in out["issues"]}
        out["issues"] += [i for i in base["issues"] if (i["where"], i["issue"]) not in seen][:10]
    return out, receipt
