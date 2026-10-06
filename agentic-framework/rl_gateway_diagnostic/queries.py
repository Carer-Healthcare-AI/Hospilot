"""Diagnostic-modality case catalog — the loader over ``cases.json``.

Same shape as ``rl_gateway/queries.py`` for beds, and deliberately a separate file rather than
a parameter on that one: the two families have different bidder rules (``icu`` is directly
eligible here, and CT alone admits ``appointments``), and a shared loader would have to carry
a flag for that difference at every call site.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_CASES_PATH = Path(__file__).with_name("cases.json")


@lru_cache(maxsize=1)
def _catalog() -> Mapping[str, Any]:
    with _CASES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def departments() -> Mapping[str, Mapping[str, str]]:
    return _catalog()["departments"]


@lru_cache(maxsize=1)
def AGENT_MAP() -> Mapping[str, str]:
    """Our department -> the engine's AgentKind.

    Unlike the bed map this is the identity for all three, because every department that can
    hold a patient is itself an eligible bidder on every modality. Kept as a map anyway so a
    future substitution has one place to live.
    """
    return {name: block["engine_kind"] for name, block in departments().items()}


@lru_cache(maxsize=1)
def NODE_TO_DEPARTMENT() -> Mapping[str, str]:
    return {block["agent_node"]: name for name, block in departments().items()}


@lru_cache(maxsize=1)
def cases() -> tuple[Mapping[str, Any], ...]:
    return tuple(_catalog()["cases"])


def default_modality() -> str:
    for case in cases():
        if case.get("default"):
            return case["modality"]
    return cases()[0]["modality"]


def modality_for(text: str) -> str | None:
    """Longest-keyword-wins match of free text to a modality.

    Longest first because ``ct`` is a substring of nothing useful but ``us`` would match
    inside ``ultrasound``; ordering by length means the most specific phrase in the order
    decides, rather than whichever case happens to be listed first.
    """
    if not text:
        return None
    haystack = f" {text.lower().strip()} "
    best: tuple[int, str] | None = None
    for case in cases():
        for keyword in case["keywords"]:
            if f" {keyword} " in haystack and (best is None or len(keyword) > best[0]):
                best = (len(keyword), case["modality"])
    return best[1] if best else None


def participants_for(modality: str) -> tuple[str, ...]:
    """The departments that bid on this modality, as OUR names."""
    for case in cases():
        if case["modality"] == modality:
            return tuple(case["participants"])
    return ()


def engine_agents_for(modality: str) -> tuple[str, ...]:
    """The same list, as the engine's AgentKind values."""
    mapping = AGENT_MAP()
    return tuple(mapping[d] for d in participants_for(modality) if d in mapping)


def known_modalities() -> tuple[str, ...]:
    return tuple(case["modality"] for case in cases())
