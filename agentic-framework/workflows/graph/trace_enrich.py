"""ID -> human-readable detail resolution for the execution trace.

`workflows.graph.trace.record_step` humanizes each task/agent result into the
trace cards the frontend shows. But activity results mostly carry *bare IDs and
counts* -- `assigned_vehicle_no: "KA-01-1234"`, `bed_ids: ["b-icu-01"]`,
`patient_token: "..."`, `approval_id: "..."` -- which mean nothing to a person
reading the trace.

This module adds one centralized, async, defensive enrichment pass. Given a
result (or input) dict it resolves the known ID-bearing keys against the SAME
Redis caches / Fabric fetchers the agents already use, and attaches a readable
sibling value (a pre-formatted string, since the trace field contract is
`{label, value: str}`) next to the raw id. `trace.to_fields` then hides the now
redundant bare-id row (see `_ENRICHED_SUPERSEDES` there).

Design notes:
  * Collect-then-batch: one pass gathers every id by type, then ONE batched read
    per type runs concurrently (`get_patients` MGET, one `get_all_beds`, one
    `get_all_ambulances`, approvals gathered). N ids -> a handful of reads.
  * Fully defensive: any miss/timeout/error degrades to the original value; this
    must NEVER break a run (mirroring `record_step`'s own discipline).
  * Never mutates the caller's object -- returns shallow-copied dicts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ENRICH_TIMEOUT = 2.0  # seconds; degrade to un-enriched on overrun

# --- key -> id-type classification -------------------------------------------
# Scalar (string-valued) keys.
_TOKEN_KEYS = frozenset({"patient_token", "token"})
_BED_ID_KEYS = frozenset({"bed_id"})
_APPROVAL_KEYS = frozenset({"approval_id"})
# vehicle keys map to the sibling name used for the resolved detail.
_VEHICLE_SIBLING = {"assigned_vehicle_no": "assigned_ambulance", "vehicle_no": "ambulance"}
# List-of-string keys.
_TOKEN_LIST_KEYS = frozenset({"patients", "patient_tokens"})
_BED_ID_LIST_KEYS = frozenset({"bed_ids"})
# The sibling key each scalar resolves into.
_TOKEN_SIBLING = "patient"
_BED_SIBLING = "bed"
_APPROVAL_SIBLING = "approval"
# When a list item is a dict already carrying one of these, it needs no name.
_NAME_KEYS = ("patient_name", "name", "full_name")


# --- formatting helpers (pure) -----------------------------------------------

def _name_display(token: str | None, patient_map: dict) -> tuple[str, str]:
    """(full_name, uhid) for a token -- replicated from agents.bed.activities so the
    trace path doesn't import the whole bed-activity module."""
    if not token:
        return "Unknown patient", ""
    p = patient_map.get(token) or {}
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"Patient {token[:8]}"
    return name, (p.get("uhid") or "")


def _patient_str(token: str, patients: dict) -> str:
    name, uhid = _name_display(token, patients)
    return f"{name} (UHID {uhid})" if uhid else name


def _bed_str(b: dict) -> str:
    label = f"{b.get('ward', '')} {b.get('bed_number', '')}".strip() or str(b.get("id", ""))
    room = b.get("room_type")
    status = b.get("status")
    if room and str(room).lower() not in label.lower():
        label = f"{label} ({room})" if label else str(room)
    return f"{label} — {status}" if status else label


def _amb_str(a: dict) -> str:
    head = str(a.get("vehicle_no", "") or "").strip() or "Ambulance"
    if a.get("vehicle_type"):
        head = f"{head} ({a['vehicle_type']})"
    segs: list[str] = []
    crew = [c for c in (a.get("driver_name"), a.get("paramedic_name")) if c]
    if crew:
        segs.append("crew " + ", ".join(str(c) for c in crew))
    if a.get("eta_mins") is not None:
        segs.append(f"ETA {a['eta_mins']} min")
    return " · ".join([head, *segs]) if segs else head


def _approval_str(row: dict, patients: dict, beds_by_id: dict) -> str:
    action = str(row.get("action_type") or "approval").replace("_", " ").capitalize()
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    who: list[str] = []
    tok = payload.get("patient_token")
    if isinstance(tok, str) and tok in patients:
        who.append(_name_display(tok, patients)[0])
    bid = payload.get("bed_id")
    if isinstance(bid, str) and bid in beds_by_id:
        who.append(_bed_str(beds_by_id[bid]))
    out = action
    if who:
        out += " for " + ", ".join(who)
    if row.get("status"):
        out += f" ({row['status']})"
    return out


def _to_dict(v: Any) -> dict | None:
    """A dataclass or dict -> plain dict (for enrichment); anything else -> None."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        try:
            return dataclasses.asdict(v)
        except Exception:  # noqa: BLE001
            return None
    return v if isinstance(v, dict) else None


def _has_name(item: dict) -> bool:
    return any(item.get(k) for k in _NAME_KEYS)


# --- public entry points ------------------------------------------------------

async def enrich_output(data: Any, ctx: dict | None = None) -> Any:
    """Return an enriched shallow copy of a result dict; never raises, never mutates."""
    d = _to_dict(data)
    if d is None:
        return data
    try:
        return await asyncio.wait_for(_enrich_dict(d, ctx or {}), timeout=_ENRICH_TIMEOUT)
    except Exception:  # noqa: BLE001 -- enrichment must never break the trace/run
        logger.debug("enrich_output degraded to raw", exc_info=True)
        return data


async def enrich_input(data: Any, ctx: dict | None = None) -> Any:
    """Enrich a task input. Unwraps the `{"args": [dataclass|dict]}` envelope that
    `run_activity` records so a single input's own fields get resolved, preserving
    the envelope so `trace._input_fields` still unwraps it."""
    if isinstance(data, dict) and set(data.keys()) == {"args"} and isinstance(data.get("args"), list):
        args = data["args"]
        if len(args) == 1:
            inner = _to_dict(args[0])
            if inner is not None:
                enriched = await enrich_output(inner, ctx)
                return {"args": [enriched]}
        return data
    return await enrich_output(data, ctx)


# --- core: collect -> batch -> build -----------------------------------------

async def _enrich_dict(d: dict, ctx: dict) -> dict:
    tokens: set[str] = set()
    bed_ids: set[str] = set()
    vehicle_nos: set[str] = set()
    approval_ids: set[str] = set()

    def _scan_item(it: dict) -> None:
        for k, v in it.items():
            if not isinstance(v, str) or not v:
                continue
            if k in _TOKEN_KEYS:
                tokens.add(v)
            elif k in _BED_ID_KEYS:
                bed_ids.add(v)
            elif k in _VEHICLE_SIBLING:
                vehicle_nos.add(v)
            elif k in _APPROVAL_KEYS:
                approval_ids.add(v)

    # One pass over top level + one level into list values.
    _scan_item(d)
    for k, v in d.items():
        if isinstance(v, list):
            if k in _TOKEN_LIST_KEYS:
                tokens.update(x for x in v if isinstance(x, str) and x)
            if k in _BED_ID_LIST_KEYS:
                bed_ids.update(x for x in v if isinstance(x, str) and x)
            for it in v:
                if isinstance(it, dict):
                    _scan_item(it)

    if not (tokens or bed_ids or vehicle_nos or approval_ids):
        return d  # nothing to resolve -- avoid any cache round-trips

    patients: dict = {}
    beds_by_id: dict = {}
    amb_by_vno: dict = {}
    approvals: dict = {}

    from cache import redis as cache
    from db.hasura import hasura
    org_id = ctx.get("org_id") or None

    # Approvals first: their payloads carry bare patient_token/bed_id, which we fold
    # into the token/bed sets so the single batched patient/bed reads cover them too.
    async def _load_approvals() -> None:
        async def _one(aid: str) -> None:
            try:
                row = await hasura.get_approval_task(aid, org_id=org_id)
            except Exception:  # noqa: BLE001
                return
            if row:
                approvals[aid] = row
                p = row.get("payload")
                if isinstance(p, dict):
                    if isinstance(p.get("patient_token"), str):
                        tokens.add(p["patient_token"])
                    if isinstance(p.get("bed_id"), str):
                        bed_ids.add(p["bed_id"])
        await asyncio.gather(*(_one(a) for a in approval_ids))

    if approval_ids:
        await _load_approvals()

    async def _load_patients() -> None:
        if not tokens:
            return
        try:
            got = await cache.get_patients(list(tokens))
        except Exception:  # noqa: BLE001
            got = {}
        missing = [t for t in tokens if t not in got]
        if missing:
            try:
                got = {**(await hasura.get_patient_names(missing)), **got}
            except Exception:  # noqa: BLE001
                pass
        patients.update(got)

    async def _load_beds() -> None:
        if not bed_ids:
            return
        try:
            allb = await cache.get_all_beds()
        except Exception:  # noqa: BLE001
            allb = []
        idx = {b.get("id"): b for b in (allb or []) if isinstance(b, dict) and b.get("id")}
        for bid in bed_ids:
            if bid in idx:
                beds_by_id[bid] = idx[bid]

    async def _load_ambulances() -> None:
        if not vehicle_nos:
            return
        try:
            alla = await cache.get_all_ambulances()
        except Exception:  # noqa: BLE001
            alla = []
        for a in alla or []:
            if isinstance(a, dict) and a.get("vehicle_no") in vehicle_nos:
                amb_by_vno[a["vehicle_no"]] = a

    await asyncio.gather(_load_patients(), _load_beds(), _load_ambulances())

    # --- build the enriched copy ---
    def _enrich_item(it: dict) -> dict:
        add: dict = {}
        if not _has_name(it):
            for tk in _TOKEN_KEYS:
                t = it.get(tk)
                if isinstance(t, str) and t in patients:
                    name, uhid = _name_display(t, patients)
                    add["patient_name"] = name
                    if uhid:
                        add.setdefault("uhid", uhid)
                    break
        if not it.get("ward"):
            b = it.get("bed_id")
            if isinstance(b, str) and b in beds_by_id:
                bd = beds_by_id[b]
                if bd.get("ward"):
                    add["ward"] = bd["ward"]
                if bd.get("bed_number"):
                    add.setdefault("bed_number", bd["bed_number"])
        if add:
            ni = dict(it)
            ni.update(add)
            return ni
        return it

    out = dict(d)
    for k, v in d.items():
        if isinstance(v, str) and v:
            if k in _TOKEN_KEYS and v in patients and _TOKEN_SIBLING not in out:
                out[_TOKEN_SIBLING] = _patient_str(v, patients)
            elif k in _BED_ID_KEYS and v in beds_by_id and _BED_SIBLING not in out:
                out[_BED_SIBLING] = _bed_str(beds_by_id[v])
            elif k in _VEHICLE_SIBLING and v in amb_by_vno:
                sib = _VEHICLE_SIBLING[k]
                if sib not in out:
                    out[sib] = _amb_str(amb_by_vno[v])
            elif k in _APPROVAL_KEYS and v in approvals and _APPROVAL_SIBLING not in out:
                out[_APPROVAL_SIBLING] = _approval_str(approvals[v], patients, beds_by_id)
        elif isinstance(v, list) and v:
            if k in _TOKEN_LIST_KEYS and "patient_details" not in out:
                details = [_patient_str(t, patients) for t in v if isinstance(t, str) and t in patients]
                if details:
                    out["patient_details"] = details
            if k in _BED_ID_LIST_KEYS and "bed_details" not in out:
                details = [_bed_str(beds_by_id[b]) for b in v if isinstance(b, str) and b in beds_by_id]
                if details:
                    out["bed_details"] = details
            if any(isinstance(it, dict) for it in v):
                new_list = [_enrich_item(it) if isinstance(it, dict) else it for it in v]
                if any(a is not b for a, b in zip(new_list, v)):
                    out[k] = new_list
    return out
