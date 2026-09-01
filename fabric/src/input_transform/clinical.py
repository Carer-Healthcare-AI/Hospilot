"""Clinical reads — orchestrate the DB's FHIR API + transform to normalized dicts.

Each function calls the DB's FHIR endpoints (via clients.fhir_client), transforms
the FHIR resources to the normal dict shapes the main backend wants (via
service.transform), and joins/computes where the DB's FHIR can't (e.g. ICU
membership from the bed→ward graph, simple aggregates).

Note: the DB's default page size is 50, so list searches pass _count=200 (the
server cap) to avoid silent truncation.

Delivery paths (see fabric/README.md for the full table): the core clinical entities
— bed, admission, visit, task, lab_order, lab_result — are BOTH streamed and served
live, and that is not redundancy. Kafka + the backend's internal DB hold per-record state,
while the routes here answer the list, filter and computed questions a per-record lookup can't:
"which ICU beds are dirty", "who is discharge-eligible", ER pressure. Same entity, two
different questions.
"""

import logging

from clients import fhir_client as fc
from input_transform import transform as tx

logger = logging.getLogger("clinical")

PAGE = 200   # DB caps _count at 200


# ─── beds ───────────────────────────────────────────────────────────────────────
def _form_code(loc) -> str | None:
    """FHIR Location.form code — 'bd' bed, 'wa' ward, 'ro' room.

    A Location models ANY place, so every query that means "beds" has to filter on
    this. tx.bed() is a mapper, not a validator: hand it a ward and it returns a
    bed-shaped dict with ward=None, which is how ward records used to leak into
    /beds/dirty.
    """
    return loc.form.coding[0].code if (loc.form and loc.form.coding) else None


async def _location_index() -> tuple[dict[str, dict], dict[str, str]]:
    """Active Locations → (beds_by_id, wards_by_id name map). Beds = form 'bd',
    wards = form 'wa' (so partOf can resolve to a ward name)."""
    locs = await fc.search_locations({"status": "active", "_count": PAGE})
    beds_raw, wards = [], {}
    for loc in locs:
        if _form_code(loc) == "wa":
            wards[loc.id] = loc.name
        else:
            beds_raw.append(loc)
    beds = {}
    for loc in beds_raw:
        b = tx.bed(loc, wards_by_id=wards)
        beds[b["id"]] = b
    return beds, wards


async def beds() -> list[dict]:
    index, _ = await _location_index()
    return list(index.values())


async def available_icu_beds() -> list[dict]:
    index, _ = await _location_index()
    return [b for b in index.values() if tx.is_icu_bed(b) and (b.get("status") == "Available")]


async def dirty_beds(icu_only: bool = False) -> list[dict]:
    """Beds awaiting housekeeping — upstream models these as status=suspended.

    Only form 'bd' Locations count. Wards are permanently suspended upstream (they
    aren't bookable places), so without this filter every ward is returned as a dirty
    bed: 8 phantom rows against 4 real ones on the reference dataset.
    """
    locs = await fc.search_locations({"status": "suspended", "_count": PAGE})
    _, wards = await _location_index()
    out = []
    for loc in locs:
        if _form_code(loc) != "bd":
            continue
        b = tx.bed(loc, wards_by_id=wards)
        if icu_only and not tx.is_icu_bed(b):
            continue
        out.append(b)
    return out


async def beds_summary() -> dict:
    index, _ = await _location_index()
    all_beds = list(index.values())
    icu = [b for b in all_beds if tx.is_icu_bed(b)]

    def _avail(rows):
        return [b for b in rows if b.get("status") == "Available"]

    total, available = len(all_beds), len(_avail(all_beds))
    icu_total, icu_avail = len(icu), len(_avail(icu))
    return {
        "total_beds": total,
        "occupied_beds": total - available,
        "available_beds": available,
        "occupancy_pct": round((total - available) / max(total, 1) * 100),
        "icu_total": icu_total,
        "icu_occupied": icu_total - icu_avail,
        "icu_available": icu_avail,
        "icu_pct": round((icu_total - icu_avail) / max(icu_total, 1) * 100),
    }


# ─── admissions ─────────────────────────────────────────────────────────────────
async def _admissions(params: dict) -> list[dict]:
    encs = await fc.search_encounters({"class": "IMP", "_count": PAGE, **params})
    index, _ = await _location_index()
    out = []
    for e in encs:
        a = tx.admission(e)
        bed = index.get(a.get("bed_id")) if a.get("bed_id") else None
        if bed:
            a["bed"] = {k: bed.get(k) for k in ("ward", "room_type", "ventilation", "features", "status")}
        out.append(a)
    return out


async def all_admissions() -> list[dict]:
    return await _admissions({})


def _is_icu(a: dict) -> bool:
    return bool(a.get("bed")) and "icu" in (a["bed"].get("ward") or "").lower()


async def icu_admissions() -> list[dict]:
    # exclude patients already pending transfer out (parity with the old query)
    return [a for a in await _admissions({"status": "in-progress"})
            if _is_icu(a) and not a.get("transfer_pending")]


async def non_icu_admissions() -> list[dict]:
    return [a for a in await _admissions({"status": "in-progress"})
            if not _is_icu(a) and not a.get("transfer_pending")]


async def admissions_with_wards() -> list[dict]:
    return await _admissions({})


async def discharge_ready_count() -> int:
    return sum(1 for a in await _admissions({"status": "in-progress"}) if a.get("discharge_ready"))


# ─── visits / ER ─────────────────────────────────────────────────────────────────
_TERMINAL_VISIT = {"completed", "cancelled"}


async def er_visits() -> list[dict]:
    # The DB returns EMER visit status as "unknown" (source status not mapped), so we
    # can't filter by ?status=in-progress — fetch all EMER and drop only terminal ones.
    encs = await fc.search_encounters({"class": "EMER", "_count": PAGE})
    visits = [tx.visit(e) for e in encs]
    return [v for v in visits if (v.get("status") or "").lower() not in _TERMINAL_VISIT]


async def er_pressure() -> dict:
    visits = await er_visits()        # already active (non-terminal)
    ctas_1_2 = sum(1 for v in visits if (v.get("triage_score") or 99) <= 2)
    ctas_3 = sum(1 for v in visits if v.get("triage_score") == 3)
    return {"ctas_1_2": ctas_1_2, "ctas_3": ctas_3, "est_admissions": ctas_1_2 + round(ctas_3 * 0.6)}


# ─── vitals ───────────────────────────────────────────────────────────────────────
async def latest_vitals(patient_token: str) -> dict | None:
    obs = await fc.search_observations({"patient": patient_token, "category": "vital-signs", "_count": PAGE})
    if not obs:
        return None
    readings = [r for r in (tx.vital(g) for g in tx.group_vitals_by_reading(obs).values()) if r]
    if not readings:
        return None
    return max(readings, key=lambda r: (r.get("recorded_at") or ""))


async def latest_vitals_bulk(tokens: list[str] | None = None) -> dict:
    """{patient_token: latest vital reading} for many patients in ONE upstream call.

    Why this exists: /vitals/latest is per-patient because `patient` is the only
    patient-scoping search param the upstream FHIR server advertises (its
    CapabilityStatement lists exactly patient, category, code, interpretation,
    _count -- there is no subject, _has, $lastn, $export or batch-Bundle support,
    all verified 404). Callers that need N patients therefore fired N calls: the
    ICU agent's escalation sweep runs one per admission, hospital-wide and
    uncapped, which is the single largest source of Fabric traffic.

    An UNFILTERED vital-signs search is the only bulk read available, and it is a
    pattern already in use here -- critical_vitals() below does the same thing
    with an interpretation filter. We group by subject and keep each patient's
    newest reading, which reproduces latest_vitals() exactly (verified against 27
    patients: identical id and recorded_at, zero mismatches).

    TRUNCATION is the hazard. This server cannot page: _offset is silently
    ignored and only a `self` link is returned, never `next`. So if the hospital
    has more vital Observations than one response carries, we get a prefix with
    no error -- and a caller would see healthy-looking data for a patient whose
    critical reading fell off the end. `complete` reports whether Bundle.total
    matched what we received; when it is False the caller MUST fall back to
    per-patient reads rather than trust this map.

    Returns {"vitals": {token: reading}, "complete": bool, "total": int|None,
             "received": int} -- shaped so the truncation signal cannot be
    ignored by accident the way a bare dict's missing keys would be.
    """
    obs, total = await fc.search_observations_with_total(
        {"category": "vital-signs", "_count": PAGE})
    received = len(obs)
    complete = (total is None) or (received >= total)
    if not complete:
        logger.warning(
            "bulk vitals TRUNCATED -- upstream reports total=%s but returned %d "
            "observations and supports no paging; callers must fall back to "
            "per-patient reads", total, received)

    latest: dict[str, dict] = {}
    for group in tx.group_vitals_by_reading(obs).values():
        r = tx.vital(group)
        token = (r or {}).get("patient_token")
        if not r or not token:
            continue
        cur = latest.get(token)
        if cur is None or (r.get("recorded_at") or "") > (cur.get("recorded_at") or ""):
            latest[token] = r

    if tokens:
        wanted = set(tokens)
        latest = {t: v for t, v in latest.items() if t in wanted}

    return {"vitals": latest, "complete": complete, "total": total, "received": received}


async def critical_vitals() -> list[dict]:
    # DB supports the interpretation=AA filter (critical-alert flagged vitals)
    obs = await fc.search_observations({"category": "vital-signs", "interpretation": "AA", "_count": PAGE})
    readings = [r for r in (tx.vital(g) for g in tx.group_vitals_by_reading(obs).values()) if r]
    return [r for r in readings if r.get("is_critical")]


# ─── nursing tasks ────────────────────────────────────────────────────────────────
async def incomplete_tasks() -> list[dict]:
    # DB Task status: completed=false -> "requested", completed=true -> "completed"
    tasks = await fc.search_tasks({"status": "requested", "_count": PAGE})
    return [tx.nursing_task(t) for t in tasks]


async def overdue_tasks() -> list[dict]:
    tasks = await fc.search_tasks({"overdue": "true", "_count": PAGE})
    return [tx.nursing_task(t) for t in tasks]


async def nursing_tasks_for(admission_id: str, statuses: str = "requested") -> list[dict]:
    # DB filters Task by the bare admission uuid via ?encounter=
    tasks = await fc.search_tasks({"encounter": admission_id, "status": statuses, "_count": PAGE})
    return [tx.nursing_task(t) for t in tasks]


async def completed_task_count(admission_id: str) -> int:
    tasks = await fc.search_tasks({"encounter": admission_id, "status": "completed", "_count": PAGE})
    return len(tasks)


# ─── labs ───────────────────────────────────────────────────────────────────────
async def lab_orders() -> list[dict]:
    # the DB's ServiceRequest status filter is an exact match (no comma-OR), so query
    # each pending status separately and merge.
    rows = []
    for status in ("active", "on-hold"):
        rows += await fc.search_service_requests({"status": status, "_count": PAGE})
    return [tx.lab_order(r) for r in rows]


async def lab_results(patient_token: str | None = None, test_code: str | None = None) -> list[dict]:
    # The upstream FHIR API requires `patient` for category=laboratory — without it
    # the DB returns 400, which propagates as a 500. Return empty rather than error.
    if not patient_token:
        return []
    params = {"category": "laboratory", "_count": PAGE, "patient": patient_token}
    if test_code:
        params["code"] = test_code
    obs = await fc.search_observations(params)
    return [tx.lab_result(o) for o in obs]


# ─── departments / patients ──────────────────────────────────────────────────────
async def departments() -> list[dict]:
    return [tx.department(o) for o in await fc.search_organizations({"_count": PAGE})]


async def patient_tokens() -> list[str]:
    return [t for t in (tx.patient_token(p) for p in await fc.search_patients({"_count": PAGE})) if t]


async def patient(token: str) -> dict | None:
    p = await fc.read_patient(token)
    return tx.patient(p) if p else None


async def patient_names(tokens: list[str]) -> dict[str, dict]:
    """{token: {first_name, last_name, uhid, ...}} — matches db.hasura.get_patient_names."""
    out: dict[str, dict] = {}
    for t in tokens:
        p = await fc.read_patient(t)
        if p:
            out[t] = tx.patient(p)
    return out


async def all_patients() -> list[dict]:
    """All patients — used by the diff poller to detect newly registered patients."""
    return [tx.patient(p) for p in await fc.search_patients({"_count": PAGE})]


async def patient_by_mobile(mobile: str) -> dict:
    """Resolve a patient from a mobile number.

    Normalises to last 10 digits, searches FHIR Patient by phone, confirms the
    match client-side (telecom digits must end with the queried digits), picks the
    best candidate (active first, then most recently updated), and enriches with
    the current in-progress ER encounter.
    """
    import re
    digits = re.sub(r"\D", "", mobile)[-10:]
    if not digits:
        return {"exists": False}

    candidates = await fc.search_patients_by_phone(digits)

    def _matches(pat) -> bool:
        for t in (getattr(pat, "telecom", None) or []):
            v = re.sub(r"\D", "", getattr(t, "value", "") or "")
            if v.endswith(digits):
                return True
        return False

    confirmed = [p for p in candidates if _matches(p)]
    if not confirmed:
        return {"exists": False}

    def _rank(p):
        active = bool(getattr(p, "active", False))
        meta = getattr(p, "meta", None)
        lu = getattr(meta, "last_updated", None) if meta else None
        return (int(active), str(lu) if lu else "")

    best = sorted(confirmed, key=_rank, reverse=True)[0]
    info = tx.patient(best)
    token = info["patient_token"]

    current_visit_id = None
    try:
        encs = await fc.search_encounters(
            {"patient": token, "class": "EMER", "status": "in-progress", "_count": 1}
        )
        if encs:
            from fhirgw.mappers._common import bare_id
            current_visit_id = bare_id(encs[0].id)
    except Exception:
        pass

    return {"exists": True, **info, "current_visit_id": current_visit_id}
