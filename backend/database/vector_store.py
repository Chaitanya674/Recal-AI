"""Vector lookups over graph nodes. Two paths with one interface:
- Firestore KNN (`find_nearest` on the nodes vector index) when a project is configured
  and real 768-dim embeddings are in play;
- exact brute-force cosine over the in-memory cache otherwise (local dev, mock mode,
  or as the automatic fallback if the server query fails).
The cache is filled by GraphStore on write and on load, so both paths see the same data."""
from __future__ import annotations

from ..helper.config import settings
from . import fire


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class VectorStore:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self._mem: dict[str, tuple[list[float], dict]] = {}

    def cache(self, item_id: str, embedding: list[float], meta: dict):
        self._mem[item_id] = (embedding, meta)

    def clear(self):
        self._mem.clear()

    def nearest(self, embedding: list[float], k: int = 3,
                kind: str | None = None) -> list[tuple[str, float, dict]]:
        if fire.enabled() and not settings.MOCK:
            try:
                where = [("kind", "==", kind)] if kind else []
                return fire.vector_nearest(("users", self.user_id, "nodes"),
                                           embedding, k, where=where)
            except Exception:  # noqa: BLE001 — e.g. vector index not created yet
                pass
        scored = [(_cos(vec, embedding), i, meta)
                  for i, (vec, meta) in self._mem.items()
                  if kind is None or meta.get("kind") == kind]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [(i, s, m) for s, i, m in scored[:k]]
