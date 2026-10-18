"""Graph store: nodes + typed edges. The in-memory dicts are the working set; with
Firestore configured they are a write-through cache — every node/edge is persisted to
users/{uid}/nodes|edges on write and the whole graph is loaded back on first access,
so the knowledge graph finally survives restarts. Embedding lookups: VectorStore."""
from __future__ import annotations

import uuid
from collections import defaultdict

from . import fire, schema
from .vector_store import VectorStore


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _vector_values(v) -> list[float]:
    """Firestore returns a Vector object; normalize to a plain list."""
    if isinstance(v, (list, tuple)):
        return list(v)
    for attr in ("value", "_value"):
        if hasattr(v, attr):
            return list(getattr(v, attr))
    return list(v)


class GraphStore:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.by_kind: dict[str, list[str]] = defaultdict(list)
        self.vectors = VectorStore(user_id)
        self._source_text: dict[str, str] = {}     # lazy cache of users/{uid}/sources
        if fire.enabled():
            self._load()

    def _load(self):
        try:
            for nid, data in fire.stream("users", self.user_id, "nodes"):
                data.pop("meta", None)
                node = {**data, "id": nid}
                if "embedding" in node:
                    node["embedding"] = _vector_values(node["embedding"])
                    self.vectors.cache(nid, node["embedding"],
                                       {"kind": node.get("kind", "")})
                self.nodes[nid] = node
                self.by_kind[node.get("kind", "?")].append(nid)
            for eid, data in fire.stream("users", self.user_id, "edges"):
                data.pop("meta", None)
                self.edges.append(data)
        except Exception:  # noqa: BLE001 — an unreachable DB = start with an empty graph
            pass

    # ---- writes ----
    def add_node(self, kind: str, embedding: list[float] | None = None, **props) -> str:
        nid = _id()
        node = {"id": nid, "kind": kind, **props}
        self.nodes[nid] = node
        self.by_kind[kind].append(nid)
        if embedding is not None:
            node["embedding"] = embedding
            self.vectors.cache(nid, embedding, {"kind": kind})
        if fire.enabled():
            try:
                doc = {k: (str(v)[:20000] if k == "snapshot" else v)
                       for k, v in node.items() if k not in ("id", "embedding")}
                if embedding is not None:
                    doc["embedding"] = fire.make_vector(embedding)
                fire.set_doc({**doc, "meta": fire.meta()},
                             "users", self.user_id, "nodes", nid)
            except Exception:  # noqa: BLE001 — memory copy stays authoritative in-session
                pass
        return nid

    def link(self, a: str, rel: str, b: str, **props):
        edge = {"from": a, "rel": rel, "to": b, **props}
        self.edges.append(edge)
        if fire.enabled():
            try:
                fire.set_doc({**edge, "meta": fire.meta()},
                             "users", self.user_id, "edges", _id())
            except Exception:  # noqa: BLE001
                pass

    def save_source_text(self, source_id: str, text: str):
        """Raw page text of a Source node. Kept off the node (users/{uid}/sources/{id})
        so loading the graph never pulls whole pages; capped under Firestore's 1 MiB."""
        self._source_text[source_id] = text
        if fire.enabled():
            try:
                fire.set_doc({"text": text[:500_000], "meta": fire.meta()},
                             *schema.sources(self.user_id), source_id)
            except Exception:  # noqa: BLE001 — memory copy stays authoritative in-session
                pass

    def source_text(self, source_id: str) -> str:
        if source_id not in self._source_text and fire.enabled():
            try:
                doc = fire.get_doc(*schema.sources(self.user_id), source_id) or {}
                self._source_text[source_id] = doc.get("text", "")
            except Exception:  # noqa: BLE001
                return ""
        return self._source_text.get(source_id, "")

    def purge(self):
        """Delete this user's graph everywhere (used when the embedding space changes)."""
        self.nodes.clear()
        self.edges.clear()
        self.by_kind.clear()
        self.vectors.clear()
        self._source_text.clear()
        if fire.enabled():
            try:
                fire.delete_col("users", self.user_id, "nodes")
                fire.delete_col("users", self.user_id, "edges")
                fire.delete_col(*schema.sources(self.user_id))
            except Exception:  # noqa: BLE001
                pass

    # ---- reads ----
    def stats(self) -> dict:
        return {"concepts": len(self.by_kind["Concept"]),
                "sources": len(self.by_kind["Source"]),
                "chunks": len(self.by_kind["Chunk"]),
                "edges": len(self.edges)}
