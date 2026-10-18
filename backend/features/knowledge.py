"""Knowledge feature: the agent's long-term memory of *content*.
Built on database.GraphStore + database.VectorStore (Firestore vector search in
real mode, in-memory otherwise). Exposes ingest_url/ingest_text/ask + get_graph().
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from urllib.parse import quote, unquote, urlparse

import httpx

from ..database.graph_store import GraphStore
from ..helper.config import settings
from ..helper import net, prompt_loader


# ---------------------------------------------------------------------------
# embeddings: Gemini on Vertex AI (shared client) / hash in mock
# ---------------------------------------------------------------------------
_EMBED_COOLDOWN_S = 600
_embed_dead_until = 0.0     # embedding-quota circuit breaker: 429 flips to hash mode


def _real_embeddings() -> bool:
    """Gemini embeddings need a Vertex client AND live quota; in MOCK or during a
    quota cooldown, hash vectors + keyword matching apply — ingestion and grounding
    must never go dark."""
    if settings.MOCK or time.time() < _embed_dead_until:
        return False
    from .llm import get_client
    return get_client() is not None


def _hash_embed(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        h = hashlib.sha256(t.lower().encode()).digest()
        v = [(b - 128) / 128 for b in h[:32]]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def _normalized(vectors: list[list[float]]) -> list[list[float]]:
    out = []
    for v in vectors:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def embed(texts: list[str]) -> list[list[float]]:
    global _embed_dead_until
    if not _real_embeddings():
        return _hash_embed(texts)
    # 768 dims: Firestore's vector index caps at 2048 (the model's default 3072 would not
    # fit) and truncated Matryoshka embeddings must be re-normalized to unit length.
    from google.genai import types as genai_types
    from .llm import get_client, with_retry
    try:
        res = with_retry(lambda: get_client().models.embed_content(
            model=EMBED_MODEL, contents=texts,
            config=genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIMS)))
        return _normalized([list(e.values) for e in res.embeddings])
    except Exception:  # noqa: BLE001 — ANY failure falls back below
        pass
    # Embeddings failed: run on hash vectors + keyword matching for a while instead
    # of going dark; the cooldown stops a stampede of doomed API calls.
    _embed_dead_until = time.time() + _EMBED_COOLDOWN_S
    return _hash_embed(texts)


EMBED_MODEL = settings.MODEL_EMBED
EMBED_DIMS = 768


def ping_embeddings() -> tuple[bool, str]:
    """One real embedding call for /api/health."""
    from .llm import get_client
    if get_client() is None:
        return False, "no Gemini client"
    try:
        res = get_client().models.embed_content(model=EMBED_MODEL, contents=["ping"])
        return True, f"{EMBED_MODEL}: {len(res.embeddings[0].values)} dims"
    except Exception as e:  # noqa: BLE001
        return False, f"{EMBED_MODEL}: {type(e).__name__}: {str(e)[:200]}"


_STOP = set("""a an and are as at be by can do does explain for from give how i in into is it
make me min minute minutes of on one or reel second seconds show tell the this to video
visual visually vs what why with""".split())


def _topic_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


class Knowledge(GraphStore):
    """GraphStore + the domain operations the agents use as tools."""

    # ---- domain writes ----
    def upsert_concept(self, label: str, domain: str = "") -> str:
        norm = label.strip().lower()
        vec = embed([norm])[0]
        for nid, score, _ in self.vectors.nearest(vec, k=1, kind="Concept"):
            node = self.nodes.get(nid)
            if node and (node["label"].lower() == norm or score > 0.86):
                return nid
        return self.add_node("Concept", embedding=vec, label=label.strip(), domain=domain)

    def add_source(self, url: str, title: str, snapshot: str) -> str:
        sid = self.add_node("Source", url=url, title=title)
        self.save_source_text(sid, snapshot)
        return sid

    def add_chunk(self, source_id: str, text: str, ordinal: int) -> str:
        cid = self.add_node("Chunk", embedding=embed([text])[0], text=text, ordinal=ordinal)
        self.link(source_id, "HAS_CHUNK", cid)
        return cid

    def add_generation(self, kind: str, output: str, reasoning: str,
                       chunk_ids: list[str]) -> str:
        # transcript lives in users/{uid}/generations (memory.record_generation); the
        # node only anchors GROUNDED_IN edges
        gid = self.add_node("Generation", gen_kind=kind)
        for c in chunk_ids:
            if c in self.nodes:
                self.link(gid, "GROUNDED_IN", c)
        return gid

    # ---- domain reads ----
    def nearest_concepts(self, text: str, k: int = 3) -> list[dict]:
        hits = self.vectors.nearest(embed([text])[0], k=k, kind="Concept")
        return [self.nodes[i] for i, _, _ in hits if i in self.nodes]

    def relevant_chunks(self, text: str, k: int = 6) -> list[dict]:
        """Chunks actually about `text`, best first; [] if nothing ingested is on-topic.
        Real embeddings: the cosine floor (REEL_MIN_SIMILARITY) is the primary gate; the
        keyword bypass needs at least HALF the topic words — one stray word ("LLM" in an
        unrelated page) used to drag reels onto the wrong source. Hash mode (MOCK /
        embedding quota cooldown): every chunk is scanned and ranked by keyword overlap, since
        hash vectors carry no meaning."""
        words = _topic_words(text)
        need = max(1, (len(words) + 1) // 2)
        scores: dict[str, float] = {}
        if _real_embeddings():
            vec = embed([text])[0]
            floor = settings.REEL_MIN_SIMILARITY
            for cid, score, _ in self.vectors.nearest(vec, k=k * 3, kind="Chunk"):
                node = self.nodes.get(cid)
                if node and (score >= floor
                             or len(words & _topic_words(node.get("text", ""))) >= need):
                    scores[cid] = max(scores.get(cid, 0.0), score)
            for nid, score, _ in self.vectors.nearest(vec, k=k, kind="Concept"):
                concept = self.nodes.get(nid)
                if concept and (score >= floor
                                or len(words & _topic_words(concept.get("label", ""))) >= need):
                    for ch in self.neighborhood([nid])["chunks"]:
                        scores[ch["id"]] = max(scores.get(ch["id"], 0.0), score)
        else:
            for cid in self.by_kind.get("Chunk", []):
                overlap = len(words & _topic_words(self.nodes[cid].get("text", "")))
                if overlap:
                    scores[cid] = max(scores.get(cid, 0.0), float(overlap))
            for nid in self.by_kind.get("Concept", []):
                overlap = len(words & _topic_words(self.nodes[nid].get("label", "")))
                if overlap:
                    for ch in self.neighborhood([nid])["chunks"]:
                        scores[ch["id"]] = max(scores.get(ch["id"], 0.0), float(overlap))
        best = sorted(scores, key=scores.get, reverse=True)[:k]
        return [self.nodes[c] for c in best]

    def neighborhood(self, concept_ids: list[str]) -> dict:
        wanted = set(concept_ids)
        chunk_ids = {e["from"] for e in self.edges
                     if e["rel"] == "MENTIONS" and e["to"] in wanted}
        return {"chunks": [self.nodes[c] for c in chunk_ids if c in self.nodes],
                "concepts": [self.nodes[c] for c in wanted if c in self.nodes]}

    def grounded(self, gen_id: str, concept_labels: list[str]) -> bool:
        labels = {l.lower() for l in concept_labels}
        gchunks = {e["to"] for e in self.edges
                   if e["rel"] == "GROUNDED_IN" and e["from"] == gen_id}
        for e in self.edges:
            if (e["rel"] == "MENTIONS" and e["from"] in gchunks
                    and self.nodes.get(e["to"], {}).get("label", "").lower() in labels):
                return True
        return bool(gchunks) and not labels

    def has_chunk(self, chunk_id: str) -> bool:
        return self.nodes.get(chunk_id, {}).get("kind") == "Chunk"

    def source_of(self, chunk_id: str) -> dict:
        """The Source node a chunk was cut from ({} if unknown)."""
        sid = next((e["from"] for e in self.edges
                    if e["rel"] == "HAS_CHUNK" and e["to"] == chunk_id), None)
        return self.nodes.get(sid, {}) if sid else {}


_stores: dict[str, Knowledge] = {}


def get_graph(user_id: str) -> Knowledge:
    """Starts empty: everything in the graph comes from sources the user ingested."""
    if user_id not in _stores:
        _stores[user_id] = Knowledge(user_id)
    return _stores[user_id]


def reset_graphs():
    """Drop all graphs — needed when the embedding space changes (mock hash vectors and
    real Gemini embeddings have different dimensions and can't be compared). With
    Firestore on, the persisted copies are purged too, or stale vectors would reload."""
    for store in _stores.values():
        store.purge()
    _stores.clear()


# ---------------------------------------------------------------------------
# ingestion + ask (agent tools)
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
# Both defined in helper/config.py (env-overridable).
WIKI_API = settings.WIKI_API_URL
WIKI_ARTICLE_BASE = settings.WIKI_ARTICLE_BASE


_CONCEPT_SPLIT = re.compile(r"\s+(?:vs\.?|versus|and|or)\s+|\s*,\s*", re.I)


def _concepts(topic: str) -> list[str]:
    """'BM25 vs embeddings' -> ['BM25', 'embeddings']: comparative/compound topics
    need one source per side or the reel can't ground both halves."""
    parts = [p.strip(" .?!") for p in _CONCEPT_SPLIT.split(topic)]
    return [p for p in parts if p][:3] or [topic.strip()]


def _wiki_query(query: str) -> tuple[str | None, str, str]:
    """One full-text Wikipedia search: (top hit as URL or None, spelling suggestion
    or '', failure reason or '')."""
    try:
        r = httpx.get(WIKI_API, timeout=10.0, headers={"User-Agent": "RecalBot/0.1"},
                      params={"action": "query", "list": "search", "srsearch": query,
                              "srlimit": 1, "format": "json"},
                      verify=net.ssl_context())
        r.raise_for_status()
        q = r.json().get("query", {})
        hits = q.get("search") or []
        if hits:
            return (WIKI_ARTICLE_BASE
                    + quote(hits[0]["title"].replace(" ", "_"))), "", ""
        return None, (q.get("searchinfo") or {}).get("suggestion", ""), ""
    except Exception as e:  # noqa: BLE001 — report, don't crash: caller surfaces the reason
        return None, "", f"{type(e).__name__}: {str(e)[:120]}"


def _wiki_search(concept: str) -> tuple[str | None, str]:
    """Typo-tolerant search: one misspelled word ('artictecture') zero-hits the whole
    query, so on empty results retry Wikipedia's own spelling suggestion, then relax
    to the concept's content words (longest first). Returns (url or None, error)."""
    url, suggestion, err = _wiki_query(concept)
    if url or err:
        return url, err
    if suggestion:                                    # "did you mean …"
        url, _, err = _wiki_query(suggestion)
        if url or err:
            return url, err
    words = sorted(_topic_words(concept), key=len, reverse=True)
    tries = [" ".join(words)]
    if len(words) >= 2:                               # a typo'd word zero-hits the query:
        tries += [" ".join(w for w in words if w != drop)   # retry with one word dropped
                  for drop in words[:3]]
    tries += words[:2]                                # last resort: the longest words alone
    seen: set[str] = set()
    for q in tries:
        if not q or q.lower() == concept.lower() or q in seen or len(seen) >= 5:
            continue                                  # cap: don't trip Wikipedia's rate limit
        seen.add(q)
        url, _, err2 = _wiki_query(q)
        if url:
            return url, ""
        err = err or err2
    return None, err


# Hosts that never make good study sources (video/social/paywalled shells).
_SKIP_HOSTS = ("duckduckgo.", "youtube.", "youtu.be", "facebook.", "x.com", "twitter.",
               "instagram.", "tiktok.", "pinterest.", "linkedin.", "amazon.", "quora.",
               "reddit.")
_RESULT_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"', re.I)


def _rank_result(url: str) -> int:
    """Higher = more useful to a developer. Official docs and tutorials beat
    encyclopedias: Wikipedia is kept but sinks to the bottom of the pile."""
    host, path = (urlparse(url).hostname or "").lower(), urlparse(url).path.lower()
    score = 0
    if host.startswith(("docs.", "developer.", "developers.", "learn.")) or host.endswith(".dev"):
        score += 3
    if any(seg in path for seg in ("/docs", "/guide", "/tutorial", "/learn", "/blog")):
        score += 2
    if host.endswith(("github.com", "github.io", "readthedocs.io")):
        score += 2
    if "wikipedia.org" in host:
        score -= 5
    return score


_UDDG_LINK = re.compile(r'href="([^"]*?uddg=[^"]+)"', re.I)


def _web_search(query: str, k: int = 4) -> tuple[list[str], str]:
    """Keyless web search via the configured HTML endpoint (DuckDuckGo by default).
    Returns (urls best-first, failure reason) — real docs/blog pages, not Wikipedia.
    DDG answers bot-suspicious networks with a 202 challenge page: report it, so the
    caller can fall back loudly instead of silently serving Wikipedia."""
    try:
        r = httpx.get(settings.WEB_SEARCH_URL, params={"q": query}, timeout=12.0,
                      follow_redirects=True, verify=net.ssl_context(),
                      headers={"User-Agent": "Mozilla/5.0 (compatible; RecalBot/0.1)"})
        if r.status_code != 200:
            return [], f"search endpoint answered HTTP {r.status_code} (bot challenge?)"
    except Exception as e:  # noqa: BLE001 — caller falls back to Wikipedia
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    urls: list[str] = []
    for href in _RESULT_LINK.findall(r.text) or _UDDG_LINK.findall(r.text):
        if href.startswith("//"):
            href = "https:" + href
        if "uddg=" in href:                     # DDG wraps targets: /l/?uddg=<urlencoded>
            href = unquote(re.search(r"uddg=([^&]+)", href).group(1))
        host = (urlparse(href).hostname or "").lower()
        if not host or any(s in host for s in _SKIP_HOSTS):
            continue
        if href not in urls:
            urls.append(href)
    urls.sort(key=_rank_result, reverse=True)
    return urls[:k], "" if urls else "no results"


def _resolve_redirect(url: str) -> str:
    """Grounding metadata URIs are Google redirect wrappers — store the real page."""
    if "grounding-api-redirect" not in url and "vertexaisearch" not in url:
        return url
    try:
        r = httpx.get(url, timeout=12.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; RecalBot/0.1)"},
                      verify=net.ssl_context())
        return str(r.url)
    except Exception:  # noqa: BLE001 — the wrapper itself still ingests fine
        return url


def _grounded_sources(topic: str, k: int = 3) -> tuple[list[str], str]:
    """Google-Search-grounded source finding (primary): the pages developers actually
    read — official docs, tutorials, engineering blogs. Needs a live Gemini client."""
    from .llm import grounded_search
    try:
        data, meta = grounded_search(prompt_loader.get("find_sources", topic=topic))
    except Exception as e:  # noqa: BLE001 — quota/offline: next search layer takes over
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    urls: list[str] = []
    for u in [str(u) for u in (data.get("urls") or []) if str(u).startswith("http")] + meta:
        u = _resolve_redirect(u)
        host = (urlparse(u).hostname or "").lower()
        if host and not any(s in host for s in _SKIP_HOSTS) and u not in urls:
            urls.append(u)
    urls.sort(key=_rank_result, reverse=True)
    return urls[:k], "" if urls else "no grounded results"


def find_sources(topic: str) -> tuple[list[str], str]:
    """(urls, problem): good web pages for the topic, plus notes on any search layer
    that failed (network/TLS/bot-challenge trouble must never be silent).
    Layers, best first: Gemini Google-Search grounding (real docs/blogs) -> keyless
    HTML web search -> Wikipedia as the last resort, flagged as a fallback.
    Used to auto-acquire sources when a reel is requested on an empty/off-topic graph —
    the reel itself is still grounded in ingested chunks, never in raw search results.
    MOCK: URLs are synthesized without a network call (ingest_url won't fetch them either)."""
    if settings.MOCK:
        return [WIKI_ARTICLE_BASE + quote(c.replace(" ", "_"))
                for c in _concepts(topic)], ""
    problems: list[str] = []
    urls, err = _grounded_sources(topic)
    if err:
        problems.append(f"grounded search: {err}")
    if not urls:
        for concept in _concepts(topic):
            hits, err = _web_search(f"{concept} explained")
            if err:
                problems.append(f"web search: {err}")
            urls += [u for u in hits[:1] if u not in urls]
    if not urls:                               # last resort, and say so
        for concept in _concepts(topic):
            url, werr = _wiki_search(concept)
            if werr:
                problems.append(f"wikipedia: {werr}")
            elif url and url not in urls:
                urls.append(url)
        if urls:
            problems.append("only Wikipedia was reachable — using it as fallback")
    return urls, "; ".join(dict.fromkeys(problems))


def ping_web() -> tuple[bool, str]:
    """One real Wikipedia search for /api/health — proves outbound HTTPS works."""
    url, err = _wiki_search("alan turing")
    return (True, url) if url else (False, err or "no results")


def _wiki_extract(url: str) -> tuple[str, str] | None:
    """Clean article plaintext for Wikipedia pages via the MediaWiki extracts API —
    generic tag-stripping leaves nav/reference noise that poisons reel chunks."""
    host = urlparse(url).hostname or ""
    if not host.endswith("wikipedia.org"):
        return None
    try:
        title = unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
        r = httpx.get(f"https://{host}/w/api.php", timeout=20.0,
                      headers={"User-Agent": "RecalBot/0.1"},
                      params={"action": "query", "prop": "extracts", "explaintext": 1,
                              "redirects": 1, "format": "json", "titles": title},
                      verify=net.ssl_context())
        r.raise_for_status()
        page = next(iter(r.json()["query"]["pages"].values()))
        text = re.sub(r"\s+", " ", page.get("extract", "")).strip()
        return (page.get("title", title).strip()[:120], text) if text else None
    except Exception:  # noqa: BLE001 — fall back to generic HTML stripping
        return None


# Page chrome that poisons chunks with nav labels, cookie banners and footers.
_CHROME = re.compile(r"<(script|style|noscript|svg|nav|header|footer|aside|form|iframe|button)\b.*?</\1\s*>",
                     re.S | re.I)
_MAIN = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_BLOCK_TAG = re.compile(r"</(p|li|h[1-6]|pre|tr|blockquote|section|div)\s*>", re.I)


def _readable_text(html: str) -> str:
    """Article text, not page text: strip chrome, prefer <article>/<main>, keep
    block boundaries as sentence breaks so chunking stays sentence-aligned."""
    import html as _html
    body = _CHROME.sub(" ", html)
    mains = [m.group(2) for m in _MAIN.finditer(body)]
    if mains:                                  # the longest content region wins
        body = max(mains, key=len)
    body = _BLOCK_TAG.sub(". ", body)          # block ends read as sentence ends
    text = _html.unescape(_TAG.sub(" ", body))
    text = re.sub(r"\s*\.(\s*\.)+", ".", text)             # collapse ". ." runs
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)]
    kept = [s for s in sentences if len(s.split()) >= 4]   # drop nav crumbs/labels
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _fetch_readable(url: str) -> tuple[str, str]:
    wiki = _wiki_extract(url)
    if wiki:
        return wiki
    r = httpx.get(url, timeout=20.0, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (compatible; RecalBot/0.1)"},
                  verify=net.ssl_context())
    r.raise_for_status()
    html = r.text
    title = (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, url])[1]
    text = _readable_text(html)
    if not text:                               # pathological markup: old crude strip
        text = re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()
    return title.strip()[:120], text


def _chunk_text(text: str, size: int = 1000, cap: int = 14000) -> list[str]:
    """Sentence-aligned ~size-char chunks. Fixed-offset slicing used to cut words in
    half, which poisoned reel callouts and narration with fragments like 'n the…'."""
    out: list[str] = []
    cur = ""
    used = 0
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if used >= cap:
            break
        if cur and len(cur) + len(sent) + 1 > size:
            out.append(cur)
            used += len(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur and used < cap:
        out.append(cur)
    return out or ([text[:size]] if text.strip() else [])


def ingest_url(user: str, url: str) -> dict:
    k = get_graph(user)
    if settings.MOCK:
        title, text = f"Docs: {url.split('/')[-1] or url}", ("Seeded mock page text. " * 80)
    else:
        title, text = _fetch_readable(url)
    sid = k.add_source(url, title, text)
    chunk_texts = _chunk_text(text)
    chunk_ids = [k.add_chunk(sid, t, n) for n, t in enumerate(chunk_texts)]
    concepts = 0
    if settings.MOCK:
        for word in title.split()[:2]:
            k.link(chunk_ids[0], "MENTIONS", k.upsert_concept(word, "ingested"), confidence=0.7)
            concepts += 1
    else:
        from .llm import route
        out, _ = route("ask", 'Extract up to 8 triples as JSON {"triples":[[a,rel,b]...]} from:\n'
                       + text[:6000])
        for triple in out.get("triples", [])[:8]:
            if not (isinstance(triple, list) and len(triple) == 3):
                continue
            a, rel, b = triple
            ka, kb = k.upsert_concept(a), k.upsert_concept(b)
            k.link(ka, (rel or "ELABORATES").upper().replace(" ", "_")[:24], kb)
            for cid, chunk in zip(chunk_ids, chunk_texts):   # link every chunk naming it
                low = chunk.lower()
                if str(a).lower() in low or str(b).lower() in low or cid == chunk_ids[0]:
                    k.link(cid, "MENTIONS", ka, confidence=0.6)
            concepts += 2
    return {"url": url, "title": title, "chunks": len(chunk_ids),
            "concepts": concepts, "linked": 0, "prereq_edges": 0}


def model_knowledge_source(user: str, topic: str) -> dict | None:
    """When every search layer is blocked or out of quota, let the model
    write what it KNOWS about the topic and ingest that as a source. The source is
    titled "Model knowledge: …" so its provenance is visible on the card, and reels
    stay grounded in ingested chunks exactly like any other source.
    Returns the ingest summary, or None when no model is reachable / output too thin."""
    from .llm import available, route
    if not available():
        return None
    out, _ = route("model_knowledge",
                   prompt_loader.get("model_knowledge", topic=topic),
                   mock_output={}, domain=topic[:24],
                   verifier=lambda o: (len(str((o or {}).get("text", "")).split()) >= 150,
                                       "explainer under 150 words"))
    text = " ".join(str(out.get("text", "")).split()) if isinstance(out, dict) else ""
    if len(text.split()) < 150:
        return None
    return ingest_text(user, text, title=f"Model knowledge: {topic}"[:120])


def ingest_text(user: str, text: str, title: str = "pasted") -> dict:
    k = get_graph(user)
    sid = k.add_source("", title, text)
    ids = [k.add_chunk(sid, t, n) for n, t in enumerate(_chunk_text(text))]
    return {"url": "", "title": title, "chunks": len(ids), "concepts": 0,
            "linked": 0, "prereq_edges": 0}


def ask(user: str, question: str) -> dict:
    """Graph-first with chunk citations; empty graph -> caller uses Search grounding."""
    k = get_graph(user)
    concepts = k.nearest_concepts(question, k=3)
    hood = k.neighborhood([c["id"] for c in concepts]) if concepts else {"chunks": []}
    if not hood["chunks"]:
        return {"answer_context": "", "citations": [], "from": "live"}
    chunks = hood["chunks"][:5]
    context = "\n".join(f"[{c['id']}] {c['text']}" for c in chunks)
    gen_id = k.add_generation("ask", question, "", [c["id"] for c in chunks])
    return {"answer_context": prompt_loader.get("ask_answer", context=context,
                                                question=question),
            "citations": [{"chunk_id": c["id"], "url": "", "title": c["text"][:60]}
                          for c in chunks],
            "from": "graph", "generation_id": gen_id,
            "concept_labels": [c["label"] for c in concepts]}
