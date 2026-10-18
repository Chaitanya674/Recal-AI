"""Conversation + card storage and pedagogy memory (plan §3.9, §4.6).

Dual-mode: Firestore when configured (write-through with lazy load-back, so chat, cards
and learned teaching styles survive restarts), in-memory dicts otherwise.

Style memory = the LLM's remembered intuition about what this learner likes. On 👍/👎 the
model's own reasoning trace is distilled into a reusable descriptor, embedded, and stored;
on a new query the nearest *related* preferences are retrieved and injected as context."""
from __future__ import annotations

import uuid
from datetime import datetime

from ..database import fire, schema
from ..helper.config import settings
from ..schemas import Card, CardRecord, Message, Receipt

_messages: dict[str, list[Message]] = {}          # "user:conv" -> messages
_cards: dict[str, dict[str, CardRecord]] = {}     # user -> card_id -> record
_convs: dict[str, dict[str, dict]] = {}           # user -> conv_id -> {title, ts}
_styles: dict[str, list[dict]] = {}               # user -> [{descriptor, polarity, domain, embedding}]
_generations: dict[str, dict] = {}                # gen_id -> {user, kind, reasoning}
_loaded: set[str] = set()                         # lazy-load markers ("u:conv", "cards:u", "convs:u")

STYLE_DEDUPE = 0.9       # cosine: same preference reinforced, not duplicated
STYLE_FLOOR = 0.3        # below this similarity a stored style is unrelated to the topic


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _mkey(user: str, conv: str) -> str:
    return f"{user}:{conv}"


def _ensure(user: str, conv: str = "main"):
    """Lazy load this conversation's messages + the user's cards from Firestore."""
    if not fire.enabled():
        return
    mkey = _mkey(user, conv)
    if mkey not in _loaded:
        _loaded.add(mkey)
        try:
            msgs = [Message.model_validate(d) for _, d in
                    fire.stream(*schema.messages(user, conv))]
            _messages[mkey] = sorted(msgs, key=lambda m: m.ts)
        except Exception:  # noqa: BLE001 — unreachable DB = start empty
            pass
    ckey = f"cards:{user}"
    if ckey not in _loaded:
        _loaded.add(ckey)
        try:
            _cards[user] = {cid: CardRecord.model_validate(d) for cid, d in
                            fire.stream(*schema.cards(user))}
        except Exception:  # noqa: BLE001
            pass


def append_message(user: str, role: str, content: str = "", action=None,
                   card_ids: list[str] | None = None,
                   receipt: Receipt | None = None, conv: str = "main") -> Message:
    _ensure(user, conv)
    msg = Message(id=_id(), role=role, content=content, action=action,
                  card_ids=card_ids or [], receipt=receipt, ts=datetime.now())
    _messages.setdefault(_mkey(user, conv), []).append(msg)
    if role == "user" and content:        # the first user message names the session
        meta = _convs.setdefault(user, {}).setdefault(
            conv, {"title": "", "ts": datetime.now().isoformat()})
        if not meta["title"]:
            meta["title"] = content[:48]
            if fire.enabled():
                try:
                    fire.set_doc({**meta, "meta": fire.meta()},
                                 *schema.conversations(user), conv, merge=True)
                except Exception:  # noqa: BLE001
                    pass
    if fire.enabled():
        try:
            fire.set_doc({**msg.model_dump(mode="json"), "meta": fire.meta()},
                         *schema.messages(user, conv), msg.id)
        except Exception:  # noqa: BLE001
            pass
    return msg


def get_history(user: str, n: int = 30,
                conv: str = "main") -> tuple[list[Message], list[CardRecord]]:
    _ensure(user, conv)
    msgs = _messages.get(_mkey(user, conv), [])[-n:]
    ids = [cid for m in msgs for cid in m.card_ids]
    cards = [_cards.get(user, {}).get(cid) for cid in ids]
    return msgs, [c for c in cards if c]


def new_conversation(user: str) -> str:
    """Start a fresh chat session; returns its id (the UI stores it in a cookie)."""
    cid = _id()
    meta = {"title": "", "ts": datetime.now().isoformat()}
    _convs.setdefault(user, {})[cid] = meta
    if fire.enabled():
        try:
            fire.set_doc({**meta, "meta": fire.meta()},
                         *schema.conversations(user), cid, merge=True)
        except Exception:  # noqa: BLE001
            pass
    return cid


def list_conversations(user: str) -> list[dict]:
    """[{id, title, ts}] newest first; 'main' always exists."""
    key = f"convs:{user}"
    if fire.enabled() and key not in _loaded:
        _loaded.add(key)
        try:
            for cid, doc in fire.stream(*schema.conversations(user)):
                _convs.setdefault(user, {}).setdefault(
                    cid, {"title": doc.get("title", ""), "ts": doc.get("ts", "")})
        except Exception:  # noqa: BLE001
            pass
    reg = _convs.setdefault(user, {})
    reg.setdefault("main", {"title": "", "ts": ""})
    out = [{"id": cid, "title": m.get("title") or ("First chat" if cid == "main" else "New chat"),
            "ts": m.get("ts", "")} for cid, m in reg.items()]
    return sorted(out, key=lambda x: x["ts"], reverse=True)


def save_card(user: str, card: Card, msg_id: str = ""):
    _ensure(user)
    rec = CardRecord(card=card, msg_id=msg_id, ts=datetime.now())
    _cards.setdefault(user, {})[card.id] = rec
    if fire.enabled():
        try:
            fire.set_doc({**rec.model_dump(mode="json"), "meta": fire.meta()},
                         *schema.cards(user), card.id)
        except Exception:  # noqa: BLE001
            pass


def get_card(user: str, card_id: str) -> CardRecord | None:
    _ensure(user)
    return _cards.get(user, {}).get(card_id)


def reset_user(user: str, conv: str = "main"):
    """Clears this conversation and the user's cards. Learned styles stay."""
    _messages.pop(_mkey(user, conv), None)
    _cards.pop(user, None)
    if fire.enabled():
        try:
            fire.delete_col(*schema.messages(user, conv))
            fire.delete_col(*schema.cards(user))
        except Exception:  # noqa: BLE001
            pass


# ---- pedagogy memory -------------------------------------------------------
def record_generation(user: str, kind: str, output: str, reasoning: str,
                      chunk_ids: list[str]) -> str:
    from .knowledge import get_graph
    gid = get_graph(user).add_generation(kind, output, reasoning,
                                         [c for c in chunk_ids if c])
    _generations[gid] = {"user": user, "kind": kind, "reasoning": reasoning,
                         "output_summary": output[:4000]}
    if fire.enabled():
        try:
            fire.set_doc({"user": user, "kind": kind, "reasoning": reasoning[:20000],
                          "output_summary": output[:1500], "meta": fire.meta()},
                         *schema.generations(user), gid)
        except Exception:  # noqa: BLE001
            pass
    return gid


def _distill(user: str, generation_id: str) -> str:
    """The output the learner reacted to (+ the model's reasoning, when recorded) ->
    a reusable teaching approach (style_distill)."""
    gen = _generations.get(generation_id)
    if gen is None and fire.enabled():
        gen = fire.get_doc(*schema.generations(user), generation_id)
    gen = gen or {}
    parts = (gen.get("reasoning", ""), gen.get("output_summary", ""))
    trace = "\n\n".join(x for x in parts if x)
    if settings.MOCK or not trace:
        return "open with a concrete failure case; one running analogy; short paragraphs"
    from ..helper import prompt_loader
    from .llm import chat_stream
    try:
        text = "".join(chat_stream(prompt_loader.get("style_distill", content=trace[:6000])))
        return " ".join(text.split())[:200] or "keep it concrete and example-first"
    except Exception:  # noqa: BLE001
        return "keep it concrete and example-first"


def _store_style(user: str, descriptor: str, polarity: str, domain: str,
                 source: str = ""):
    from .knowledge import embed
    vec = embed([f"{descriptor} (topic: {domain})"])[0]
    if fire.enabled():
        try:
            if not settings.MOCK:   # dedupe: reinforce a near-identical preference
                for sid, sim, _ in fire.vector_nearest(
                        schema.style_memory(user), vec, 1,
                        where=[("polarity", "==", polarity)]):
                    if sim >= STYLE_DEDUPE:
                        fire.incr({"reinforced": 1}, *schema.style_memory(user), sid)
                        return
            fire.set_doc({"descriptor": descriptor, "polarity": polarity, "domain": domain,
                          "embedding": fire.make_vector(vec), "source_generation": source,
                          "reinforced": 1, "meta": fire.meta()},
                         *schema.style_memory(user), _id())
        except Exception:  # noqa: BLE001
            pass
    _styles.setdefault(user, []).append(
        {"descriptor": descriptor, "polarity": polarity, "domain": domain, "embedding": vec})


def feedback(user: str, generation_id: str, signal: str, note: str = "") -> str | None:
    """👍 -> the approach that worked is preferred; 👎 -> the approach is avoided (or the
    learner's own note, when given). Returns the stored descriptor for the UI."""
    if signal == "down":
        desc = note or _distill(user, generation_id)
        _store_style(user, desc, "avoid", "any", generation_id)
        return desc
    desc = _distill(user, generation_id)
    _store_style(user, desc, "prefer", "any", generation_id)
    return desc


def styles_for(user: str, domain: str, k: int = 3) -> list[str]:
    """Preferences related to this topic — vector-recalled when Firestore + real
    embeddings are on, most-recent-first list otherwise."""
    if fire.enabled() and not settings.MOCK:
        try:
            from .knowledge import embed
            hits = fire.vector_nearest(schema.style_memory(user), embed([domain])[0],
                                       k, where=[("polarity", "==", "prefer")])
            out = [d["descriptor"] for _, sim, d in hits if sim >= STYLE_FLOOR]
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    return [s["descriptor"] for s in _styles.get(user, [])
            if s["polarity"] == "prefer"][-k:]


def avoids_for(user: str, domain: str) -> list[str]:
    if fire.enabled():
        try:
            return [d["descriptor"] for _, d in
                    fire.stream(*schema.style_memory(user),
                                where=[("polarity", "==", "avoid")])][:5]
        except Exception:  # noqa: BLE001
            pass
    return [s["descriptor"] for s in _styles.get(user, []) if s["polarity"] == "avoid"]
