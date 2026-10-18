"""Firestore layout (schema v2) — the ONE place collection paths are defined.

Every feature builds paths with these helpers instead of hand-written strings, so the
layout can be read, reviewed and migrated in one file. Each helper returns the
collection path as a tuple; append a document id for a document:

    fire.set_doc(data, *schema.events(uid), event_id)
    fire.stream(*schema.events(uid))

users/{uid}                              profile: schema_v, created_at, last_seen
  conversations/{conv_id}                {title, ts}
    messages/{msg_id}                    Message (role, content, card_ids, receipt, ts)
  cards/{card_id}                        CardRecord (card, msg_id, conv_id, ts)
  events/{event_id}                      calendar event (start, end, kind, status, topic…)
  progress/{topic_slug}                  study-book progress {topic, gen_id, done, total}
  docs/{doc_id}                          uploaded document {name, kind, section_count}
    sections/{n}                         parsed section (title, lines, notes, headers…)
  rehearse/{doc_id}                      asked questions (TTL-style expires_at)
  nodes/{node_id}                        knowledge graph node: Concept | Chunk (+ embedding,
                                         vector-indexed) | Source (url, title) | Generation
  edges/{edge_id}                        {from, rel, to, confidence}
  sources/{node_id}                      raw page text of a Source node (never bulk-loaded)
  generations/{gen_id}                   {kind, reasoning, output_summary} for style memory
  style_memory/{style_id}                learned teaching preference (+ embedding)
  connections/{provider}                 OAuth tokens — sealed (see fire.seal)
reels/{gen_id}                           reel package; top-level so media routes resolve
                                         by id alone; `user` field = owner
config/app                               runtime key/value settings (helper/appconfig;
                                         secrets sealed via fire.seal)
config/schema                            {version, migrated_at}
prompts/{name}                           optional prompt overrides (PROMPTS_FROM_DB=1)

Binary media lives in Cloud Storage under users/{uid}/... (Firestore caps docs at 1 MiB).
"""
from __future__ import annotations

SCHEMA_V = 2


def user(uid: str) -> tuple:
    return ("users", uid)


def _under(uid: str, name: str) -> tuple:
    return (*user(uid), name)


def conversations(uid: str) -> tuple:
    return _under(uid, "conversations")


def messages(uid: str, conv: str) -> tuple:
    return (*conversations(uid), conv, "messages")


def cards(uid: str) -> tuple:
    return _under(uid, "cards")


def events(uid: str) -> tuple:
    return _under(uid, "events")


def progress(uid: str) -> tuple:
    return _under(uid, "progress")


def docs(uid: str) -> tuple:
    return _under(uid, "docs")


def doc_sections(uid: str, doc_id: str) -> tuple:
    return (*docs(uid), doc_id, "sections")


def rehearse(uid: str) -> tuple:
    return _under(uid, "rehearse")


def nodes(uid: str) -> tuple:
    return _under(uid, "nodes")


def edges(uid: str) -> tuple:
    return _under(uid, "edges")


def sources(uid: str) -> tuple:
    return _under(uid, "sources")


def generations(uid: str) -> tuple:
    return _under(uid, "generations")


def style_memory(uid: str) -> tuple:
    return _under(uid, "style_memory")


def connections(uid: str) -> tuple:
    return _under(uid, "connections")


REELS = ("reels",)
APP_CONFIG = ("config", "app")
SCHEMA_DOC = ("config", "schema")
PROMPTS = ("prompts",)

# every per-user subcollection (top level of users/{uid}); used by deletes and migrations
USER_COLLECTIONS = ("conversations", "cards", "events", "progress", "docs", "rehearse",
                    "nodes", "edges", "sources", "generations", "style_memory", "connections")
