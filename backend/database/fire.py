"""Firestore / Cloud Storage / Cloud KMS access layer (docs/firestore-design.md).

Every feature module asks `fire.enabled()` and takes its Firestore branch when a GCP
project is configured; otherwise the local fallback (SQLite / in-memory) runs. All
Firestore touching goes through the small functions here, so tests can fake this module
without faking the Google SDKs.

Path convention: positional segments alternate collection/document, e.g.
    get_doc("users", uid, "events", eid)
    stream("users", uid, "edges", where=[("rel", "==", "MENTIONS")])
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from ..helper.config import settings

SCHEMA_V = 1


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """Firestore is the store of record when a project is configured."""
    if not settings.GOOGLE_CLOUD_PROJECT:
        return False
    try:
        import google.cloud.firestore  # noqa: F401
        return True
    except ImportError:
        return False


def media_enabled() -> bool:
    if not (enabled() and settings.RECAL_MEDIA_BUCKET):
        return False
    try:
        import google.cloud.storage  # noqa: F401
        return True
    except ImportError:
        return False


def kms_enabled() -> bool:
    if not settings.RECAL_KMS_KEY:
        return False
    try:
        import google.cloud.kms  # noqa: F401
        return True
    except ImportError:
        return False


@lru_cache(maxsize=1)
def _db():
    from google.cloud import firestore
    return firestore.Client(project=settings.GOOGLE_CLOUD_PROJECT)


def _ref(path: tuple):
    ref = _db()
    for i, seg in enumerate(path):
        ref = ref.collection(seg) if i % 2 == 0 else ref.document(seg)
    return ref


def meta(by: str = "api") -> dict:
    return {"v": SCHEMA_V, "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": by}


# ---------------------------------------------------------------------------
# documents + queries
# ---------------------------------------------------------------------------
def get_doc(*path: str) -> dict | None:
    snap = _ref(path).get()
    return snap.to_dict() if snap.exists else None


def set_doc(data: dict, *path: str, merge: bool = False):
    _ref(path).set(data, merge=merge)


def delete_doc(*path: str):
    _ref(path).delete()


def stream(*colpath: str, where: list[tuple] = ()) -> list[tuple[str, dict]]:
    """All (id, data) in a collection, optionally filtered by equality/inequality."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    q = _ref(colpath)
    for field, op, value in where:
        q = q.where(filter=FieldFilter(field, op, value))
    return [(d.id, d.to_dict()) for d in q.stream()]


def delete_col(*colpath: str, where: list[tuple] = ()):
    for doc_id, _ in stream(*colpath, where=where):
        _ref((*colpath, doc_id)).delete()


def incr(fields: dict, *path: str):
    """Atomic counters, e.g. the daily usage rollup."""
    from google.cloud import firestore
    _ref(path).set({k: firestore.Increment(v) for k, v in fields.items()}, merge=True)


# ---------------------------------------------------------------------------
# vectors (KNN; indexes created per docs/firestore-design.md)
# ---------------------------------------------------------------------------
def make_vector(values: list[float]):
    from google.cloud.firestore_v1.vector import Vector
    return Vector(values)


def vector_nearest(colpath: tuple, values: list[float], k: int,
                   where: list[tuple] = ()) -> list[tuple[str, float, dict]]:
    """KNN hits as (id, cosine similarity, data) — similarity = 1 − cosine distance."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
    from google.cloud.firestore_v1.vector import Vector
    q = _ref(tuple(colpath))
    for field, op, value in where:
        q = q.where(filter=FieldFilter(field, op, value))
    hits = q.find_nearest(vector_field="embedding", query_vector=Vector(values),
                          distance_measure=DistanceMeasure.COSINE, limit=k,
                          distance_result_field="__dist").get()
    out = []
    for d in hits:
        data = d.to_dict()
        dist = data.pop("__dist", None)
        out.append((d.id, 1.0 - dist if dist is not None else 0.0, data))
    return out


# ---------------------------------------------------------------------------
# envelope encryption for secrets (TSS layer 3): OAuth tokens are sealed
# before they touch Firestore, so DB read access alone never yields a secret.
# ---------------------------------------------------------------------------
_STRENGTH = {"none": 0, "aes-gcm": 1, "kms": 2}


def encryption_mode() -> str:
    """Strongest sealing available: Cloud KMS > AES-256-GCM (RECAL_ENCRYPTION_KEY) > none."""
    if kms_enabled():
        return "kms"
    return "aes-gcm" if settings.RECAL_ENCRYPTION_KEY else "none"


def needs_reseal(sealed: dict) -> bool:
    """True when a stored secret was sealed weaker than what's configured now."""
    return _STRENGTH.get(sealed.get("enc", "none"), 0) < _STRENGTH[encryption_mode()]


@lru_cache(maxsize=1)
def _aes():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    # standard (+/) or URL-safe (-_) base64 — Fernet.generate_key() emits the latter
    raw = settings.RECAL_ENCRYPTION_KEY.strip().replace("-", "+").replace("_", "/")
    key = base64.b64decode(raw + "=" * (-len(raw) % 4))
    if len(key) != 32:
        raise ValueError("RECAL_ENCRYPTION_KEY must be 32 bytes, base64-encoded")
    return AESGCM(key)


def seal(plaintext: str) -> dict:
    raw = plaintext.encode()
    mode = encryption_mode()
    if mode == "kms":
        from google.cloud import kms
        out = kms.KeyManagementServiceClient().encrypt(
            request={"name": settings.RECAL_KMS_KEY, "plaintext": raw})
        return {"enc": "kms", "data": base64.b64encode(out.ciphertext).decode()}
    if mode == "aes-gcm":
        nonce = os.urandom(12)
        return {"enc": "aes-gcm",
                "data": base64.b64encode(nonce + _aes().encrypt(nonce, raw, None)).decode()}
    # no key configured: stored obfuscated-only, flagged so /api/health can warn
    return {"enc": "none", "data": base64.b64encode(raw).decode()}


def unseal(sealed: dict) -> str:
    raw = base64.b64decode(sealed.get("data", ""))
    if sealed.get("enc") == "kms":
        from google.cloud import kms
        out = kms.KeyManagementServiceClient().decrypt(
            request={"name": settings.RECAL_KMS_KEY, "ciphertext": raw})
        return out.plaintext.decode()
    if sealed.get("enc") == "aes-gcm":
        return _aes().decrypt(raw[:12], raw[12:], None).decode()
    return raw.decode()


# ---------------------------------------------------------------------------
# Cloud Storage (binary media; Firestore docs cap at 1 MiB)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _bucket():
    from google.cloud import storage
    return storage.Client(project=settings.GOOGLE_CLOUD_PROJECT) \
        .bucket(settings.RECAL_MEDIA_BUCKET)


def blob_upload(rel: str, data: bytes, mime: str):
    _bucket().blob(rel).upload_from_string(data, content_type=mime)


def blob_download(rel: str) -> bytes | None:
    blob = _bucket().blob(rel)
    return blob.download_as_bytes() if blob.exists() else None


def blob_exists(rel: str) -> bool:
    return _bucket().blob(rel).exists()


def blob_signed_url(rel: str, filename: str = "", minutes: int = 15) -> str:
    """V4 signed URL. Raises if the credentials can't sign (e.g. plain user ADC
    without the IAM Credentials API) — callers fall back to proxying the bytes."""
    kw = {"version": "v4", "expiration": timedelta(minutes=minutes)}
    if filename:
        kw["response_disposition"] = f'attachment; filename="{filename}"'
    return _bucket().blob(rel).generate_signed_url(**kw)
