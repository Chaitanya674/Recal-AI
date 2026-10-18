"""Prompt registry (helper rule #5): prompts.yml is the source of truth; optional
Firestore overrides (collection `prompts`, doc id = key, field `text`) when
PROMPTS_FROM_DB=1 — lets you tune prompts in prod without a redeploy."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_PROMPTS: dict[str, str] = yaml.safe_load(
    (Path(__file__).parent / "prompts.yml").read_text(encoding="utf-8"))
_USE_DB = os.getenv("PROMPTS_FROM_DB", "") == "1"


class _Safe(dict):
    def __missing__(self, k):  # leave unknown {placeholders} intact
        return "{" + k + "}"


def get(name: str, **vars) -> str:
    """Outside content (guardrails.UNTRUSTED_VARS) is wrapped as <untrusted_data> and
    the prompt gets the standing data-not-instructions rule on top."""
    from .guardrails import DATA_RULE, UNTRUSTED_VARS, wrap_untrusted
    text = _PROMPTS.get(name, "")
    if _USE_DB:
        try:
            from ..database import fire, schema
            if fire.enabled():
                doc = fire.get_doc(*schema.PROMPTS, name)
                text = (doc or {}).get("text") or text
        except Exception:  # noqa: BLE001 — yml is always the fallback
            pass
    wrapped = {k for k in UNTRUSTED_VARS & vars.keys() if "{" + k + "}" in text}
    vars = {k: wrap_untrusted(v) if k in wrapped else v for k, v in vars.items()}
    return (DATA_RULE if wrapped else "") + text.format_map(_Safe(vars))


def names() -> list[str]:
    return sorted(_PROMPTS)
