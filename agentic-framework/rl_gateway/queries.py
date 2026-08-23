"""Bed-allocation case catalog — the loader over cases.json.

cases.json is the single source of truth for WHICH resources can be auctioned, WHICH
departments participate in each, and keyword hints for resolving a resource. This module
reads it once and derives the lookups the adapter needs:

    resource -> unit                (unit_for_resource)
    resource -> participants        (participants_for — the local driver for who bids)
    department -> engine AgentKind   (AGENT_MAP, e.g. icu -> ward)
    flow node id -> department       (AGENT_NODE_TO_DEPT, e.g. icu_agent -> icu)

Add a case = add a block in cases.json; nothing here changes. There are NO canned query
strings any more — the flow's resource is chosen directly (rl_gateway.select), and the NL
query sent to the engine is synthesized from the unit.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CASES_PATH = Path(__file__).with_name("cases.json")

# Returned by the resource selector when the flow is NOT asking for a bed.
NO_BED_NEEDED = "none"


@lru_cache(maxsize=1)
def _config() -> dict:
    data = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
    if not data.get("cases"):
        raise RuntimeError("cases.json contains no cases")
    if not data.get("departments"):
        raise RuntimeError("cases.json contains no departments")
    return data


def _cases() -> list[dict]:
    return _config()["cases"]


def _departments() -> dict[str, dict]:
    return _config()["departments"]


def _case(resource: str) -> dict | None:
    return next((c for c in _cases() if c["resource"] == resource), None)


def _default_case() -> dict:
    return next((c for c in _cases() if c.get("default")), _cases()[0])


# -- public catalog ---------------------------------------------------------------------------

def resource_ids() -> list[str]:
    """All auctionable resource ids, in config order."""
    return [c["resource"] for c in _cases()]


def resource_options() -> list[tuple[str, str]]:
    """(resource, unit) pairs for building the selector prompt."""
    return [(c["resource"], c["unit"]) for c in _cases()]


def is_resource(resource: str | None) -> bool:
    return bool(resource) and _case(resource) is not None


def unit_for_resource(resource: str) -> str:
    """The engine unit whose beds this resource draws on."""
    c = _case(resource)
    if c:
        return c["unit"]
    return resource[:-4] if resource.endswith("_bed") else resource


def participants_for(resource: str) -> list[str]:
    """Departments that may bid on this resource — the local driver for who competes."""
    c = _case(resource)
    return list(c.get("participants", [])) if c else []


def resource_for_text(text: str) -> str:
    """Deterministic keyword fallback: resolve free text to a resource (default case last)."""
    q = (text or "").lower()
    for c in _cases():
        if c.get("default"):
            continue
        if any(kw in q for kw in c.get("keywords", [])):
            return c["resource"]
    return _default_case()["resource"]


# -- department maps (single source: cases.json `departments`) --------------------------------

# department -> engine AgentKind (e.g. icu -> ward). Re-exported by config.py.
AGENT_MAP: dict[str, str] = {d: v["engine_kind"] for d, v in _departments().items()}

# flow graph node id -> department (e.g. icu_agent -> icu). Replaces strategy._AGENT_DEPT.
AGENT_NODE_TO_DEPT: dict[str, str] = {v["agent_node"]: d for d, v in _departments().items()}
