"""Runtime settings that must survive restarts (e.g. study-material progress).

Dual-mode like the rest of storage: the Firestore doc `config/app` when a GCP project is
configured (secrets envelope-encrypted via fire.seal), the local SQLite file otherwise.
The local file is excluded from git and the Docker image; values there are plain text —
fine for a local or single-team deployment, not for a multi-tenant one."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..database import fire, schema

DB = Path(__file__).resolve().parent.parent / "features" / "recal.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS app_config(k TEXT PRIMARY KEY, v TEXT)")
    return conn


def get(key: str, default: str = "") -> str:
    try:
        if fire.enabled():
            doc = fire.get_doc(*schema.APP_CONFIG) or {}
            val = doc.get(key, default)
            if not (isinstance(val, dict) and "enc" in val):
                return val
            plain = fire.unseal(val)
            if fire.needs_reseal(val):          # upgrade older, weaker sealing in place
                put(key, plain, secret=True)
            return plain
        with _db() as c:
            row = c.execute("SELECT v FROM app_config WHERE k=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:  # noqa: BLE001 — config reads must never kill startup
        return default


def put(key: str, value: str, secret: bool = False):
    if fire.enabled():
        fire.set_doc({key: fire.seal(value) if secret else value, "meta": fire.meta()},
                     *schema.APP_CONFIG, merge=True)
    else:
        with _db() as c:
            c.execute("INSERT OR REPLACE INTO app_config VALUES(?,?)", (key, value))


def drop(key: str):
    if fire.enabled():
        from google.cloud import firestore
        fire.set_doc({key: firestore.DELETE_FIELD}, *schema.APP_CONFIG, merge=True)
    else:
        with _db() as c:
            c.execute("DELETE FROM app_config WHERE k=?", (key,))
