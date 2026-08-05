import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from fhirgw import repository as repo
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _has_feature(b: dict, key: str) -> bool:
    """features can arrive as a list ["ventilator"] or dict {"ventilator": true}."""
    f = b.get("features")
    if isinstance(f, list):
        return key in f
    if isinstance(f, dict):
        return bool(f.get(key))
    return False


# -- Input dataclasses ----------------------------------------------------------

@dataclass
class QueryBedsInput:
    session_id: str
    bed_type: str = ""           # "ICU" | "HDU" | "General" | "" (all)
    status: str = "Available"
    isolation_required: bool = False
    known_zero_clean: bool = False  # G47: goal already asserts zero clean beds -- skip the scan


@dataclass
class FilterBedsInput:
    session_id: str
    candidates: list = field(default_factory=list)


@dataclass
class HoldBedInput:
    session_id: str
    bed_id: str
    patient_token: str = ""


@dataclass
class PredictSaturationInput:
    session_id: str
    icu_beds: list = field(default_factory=list)


@dataclass
class NotifyInput:
    session_id: str
    message: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class SyncBedStatusInput:
    session_id: str
    bed_ids: list = field(default_factory=list)
    status: str = "reserved"


@dataclass
class BedReadinessInput:
    session_id: str
    beds: list = field(default_factory=list)   # bed records (or bare ids) being recovered


# -- sa_bed_availability --------------------------------------------------------

@activity.defn
async def query_beds(inp: QueryBedsInput) -> dict:
    """Parameterised replacement for find_available_beds. Filters by type, status, isolation."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_availability"})

    # G47: when the goal explicitly states there are zero clean beds, the
    # clean-bed search is guaranteed to return empty. Skip the Redis scan and
    # return known-zero counts directly -- the dirty-bed recovery conditions
    # (ta_query_beds.icu_count / candidate_count == 0) still resolve, so the
    # flow starts from dirty-bed recovery instead of a redundant empty search.
    if inp.known_zero_clean:
        result = {
            "candidate_count": 0, "icu_count": 0, "hdu_count": 0,
            "general_count": 0, "ventilator_count": 0, "isolation_count": 0,
            "candidates": [],
        }
        await broadcast(inp.session_id, {
            "type": "sub_agent_completed",
            "sub_agent": "sa_bed_availability",
            "result": {k: v for k, v in result.items() if k != "candidates"},
        })
        logger.info("query_beds  session=%s  known_zero_clean -> skipped scan (0 clean beds)", inp.session_id)
        return result

    all_beds = await cache.get_all_beds()

    def matches(b: dict) -> bool:
        if b.get("status") != inp.status:
            return False
        if not b.get("is_active", True):
            return False
        if inp.bed_type:
            ward = (b.get("ward") or "").upper()
            if inp.bed_type.upper() == "ICU" and "ICU" not in ward:
                return False
            if inp.bed_type.upper() == "HDU" and "HDU" not in ward and "HIGH" not in ward:
                return False
        if inp.isolation_required and not (
            b.get("room_type", "").lower() in ("isolation", "side_room", "negative_pressure")
            or _has_feature(b, "isolation")
        ):
            return False
        return True

    candidates = [b for b in all_beds if matches(b)]

    def _ward_upper(b: dict) -> str:
        return (b.get("ward") or "").upper()

    icu_count        = sum(1 for b in candidates if "ICU" in _ward_upper(b))
    hdu_count        = sum(1 for b in candidates if "HDU" in _ward_upper(b) or "HIGH" in _ward_upper(b))
    general_count    = sum(1 for b in candidates if "ICU" not in _ward_upper(b) and "HDU" not in _ward_upper(b) and "HIGH" not in _ward_upper(b))
    ventilator_count = sum(1 for b in candidates if b.get("ventilation") or _has_feature(b, "ventilator"))
    isolation_count  = sum(1 for b in candidates if b.get("room_type", "").lower() in ("isolation", "side_room", "negative_pressure") or _has_feature(b, "isolation"))

    result = {
        "candidate_count":  len(candidates),
        "icu_count":         icu_count,
        "hdu_count":         hdu_count,
        "general_count":     general_count,
        "ventilator_count":  ventilator_count,
        "isolation_count":   isolation_count,
        "candidates":        candidates,
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_availability",
        "result": {k: v for k, v in result.items() if k != "candidates"},
    })
    logger.info(
        "query_beds  session=%s  total=%d  icu=%d  hdu=%d  general=%d  vent=%d  iso=%d",
        inp.session_id, len(candidates), icu_count, hdu_count, general_count, ventilator_count, isolation_count,
    )
    return result


@activity.defn
async def check_dirty_icu_beds(session_id: str) -> dict:
    """Find dirty ICU beds that could be fast-tracked to clean (FHIR Location suspended)."""
    dirty = await repo.dirty_beds(icu_only=True)
    logger.info("check_dirty_icu_beds  session=%s  dirty=%d", session_id, len(dirty))
    return {"dirty_count": len(dirty), "dirty_beds": dirty}


@activity.defn
async def check_dirty_soon_to_release(session_id: str) -> dict:
    """Beds expected to free up soon: currently-dirty beds (FHIR Location suspended)
    plus occupied beds whose admission is already flagged discharge-ready. Each carries
    a release_signal so the caller can tell an imminently-free bed from one that simply
    needs cleaning."""
    dirty = await repo.dirty_beds(icu_only=False)
    beds = [{**b, "release_signal": "dirty"} for b in dirty]
    seen = {b.get("id") for b in beds}

    admissions = await cache.get_all_admissions()
    bed_lookup = {(b.get("id") or b.get("bed_id")): b for b in (await cache.get_all_beds())}
    for adm in admissions:
        if not adm.get("discharge_ready"):
            continue
        bed_id = adm.get("bed_id")
        if not bed_id or bed_id in seen:
            continue
        bed_rec = bed_lookup.get(bed_id) or {"id": bed_id}
        beds.append({**bed_rec, "release_signal": "discharge_ready", "admission_id": adm.get("id")})
        seen.add(bed_id)

    logger.info("check_dirty_soon_to_release  session=%s  beds=%d  dirty=%d", session_id, len(beds), len(dirty))
    return {"beds": beds}


@activity.defn
async def check_overflow_candidates(session_id: str) -> dict:
    """Available beds in alternate (non-ICU) wards that can absorb overflow when a
    primary ward is full. Specialised ICU beds are excluded -- they are reserved for
    ICU patients, not generic overflow capacity. Beds explicitly flagged for
    overflow/surge are always included."""
    all_beds = await cache.get_all_beds()

    def _is_overflow(b: dict) -> bool:
        if b.get("status") != "Available" or not b.get("is_active", True):
            return False
        room = (b.get("room_type") or "").lower()
        if room in ("overflow", "surge") or _has_feature(b, "overflow"):
            return True
        return "ICU" not in (b.get("ward") or "").upper()

    candidates = [b for b in all_beds if _is_overflow(b)]
    by_ward: dict = {}
    for b in candidates:
        w = b.get("ward") or "Unknown"
        by_ward[w] = by_ward.get(w, 0) + 1
    logger.info("check_overflow_candidates  session=%s  candidates=%d  wards=%d", session_id, len(candidates), len(by_ward))
    return {"candidates": candidates, "alternate_wards": by_ward}


@activity.defn
async def check_temporary_overflow_beds(session_id: str) -> dict:
    """Emergency fallback when no standard beds are free at all. Surfaces temporary
    surge capacity from Redis: active beds in recovery / PACU / observation / day-care
    /step-down areas, plus dirty beds that could be fast-track cleaned. Last-resort
    placement -- each candidate carries an overflow_source tag."""
    all_beds = await cache.get_all_beds()
    SURGE_WARDS = ("RECOVERY", "PACU", "OBSERVATION", "DAY", "TRANSIT", "STEP")
    surge, fast_track = [], []
    for b in all_beds:
        if not b.get("is_active", True):
            continue
        ward = (b.get("ward") or "").upper()
        room = (b.get("room_type") or "").lower()
        status = b.get("status")
        if status == "Available" and (
            any(w in ward for w in SURGE_WARDS)
            or room in ("recovery", "pacu", "observation", "overflow", "surge")
        ):
            surge.append({**b, "overflow_source": "surge_area"})
        elif status in ("Dirty", "Cleaning"):
            fast_track.append({**b, "overflow_source": "fast_track_clean"})
    candidates = surge + fast_track
    logger.info("check_temporary_overflow_beds  session=%s  surge=%d  fast_track=%d", session_id, len(surge), len(fast_track))
    return {"candidates": candidates, "surge_count": len(surge), "fast_track_count": len(fast_track)}


# -- sa_bed_ranking -------------------------------------------------------------

@activity.defn
async def filter_ventilator_beds(inp: FilterBedsInput) -> dict:
    """Filter candidates to ventilator-enabled beds only."""
    candidates = [
        b for b in inp.candidates
        if b.get("ventilation") or _has_feature(b, "ventilator")
    ]
    logger.info("filter_ventilator_beds  session=%s  in=%d  out=%d", inp.session_id, len(inp.candidates), len(candidates))
    return {"candidates": candidates}


@activity.defn
async def filter_isolation_beds(inp: FilterBedsInput) -> dict:
    """Filter candidates to isolation-capable beds (side room or negative pressure)."""
    candidates = [
        b for b in inp.candidates
        if b.get("room_type", "").lower() in ("isolation", "side_room", "negative_pressure")
        or _has_feature(b, "isolation")
    ]
    logger.info("filter_isolation_beds  session=%s  in=%d  out=%d", inp.session_id, len(inp.candidates), len(candidates))
    return {"candidates": candidates}


@activity.defn
async def apply_gender_filter(inp: FilterBedsInput) -> dict:
    """Stub -- gender-bay filtering (no gender field on bed schema yet)."""
    logger.warning("apply_gender_filter  session=%s  STUB -- bed schema has no gender field", inp.session_id)
    return {"candidates": inp.candidates}


@activity.defn
async def apply_isolation_room_filter(inp: FilterBedsInput) -> dict:
    """Filter candidates to negative-pressure isolation rooms only."""
    candidates = [
        b for b in inp.candidates
        if b.get("room_type", "").lower() == "negative_pressure"
        or _has_feature(b, "negative_pressure")
    ]
    logger.info("apply_isolation_room_filter  session=%s  in=%d  out=%d", inp.session_id, len(inp.candidates), len(candidates))
    return {"candidates": candidates}


@activity.defn
async def trigger_alternate_ward_search(inp: FilterBedsInput) -> dict:
    """Search alternate wards when primary ward is full -- returns all available beds."""
    all_beds = await cache.get_all_beds()
    candidates = [
        b for b in all_beds
        if b.get("status") == "Available" and b.get("is_active", True)
    ]
    logger.info("trigger_alternate_ward_search  session=%s  candidates=%d", inp.session_id, len(candidates))
    return {"candidates": candidates}


@activity.defn
async def recommend_transfer_allocation(session_id: str) -> dict:
    """Stub -- inter-ward / inter-facility transfer recommendation (no transfer data yet)."""
    logger.warning("recommend_transfer_allocation  session=%s  STUB", session_id)
    return {"recommendation": None}


@activity.defn
async def recommend_icu_to_ward_transfer(session_id: str) -> dict:
    """Stub -- ICU step-down recommendation to free an ICU bed (no ICU context here yet)."""
    logger.warning("recommend_icu_to_ward_transfer  session=%s  STUB", session_id)
    return {"recommendation": None}


@activity.defn
async def allocate_overflow_bed(session_id: str) -> dict:
    """Stub -- overflow bed allocation (no overflow ward mapping yet)."""
    logger.warning("allocate_overflow_bed  session=%s  STUB", session_id)
    return {"bed_id": None}


# -- sa_bed_reservation ---------------------------------------------------------

@activity.defn
async def sync_bed_status(inp: SyncBedStatusInput) -> dict:
    """Reconcile Redis bed records to the post-reservation status and write audit. The
    Fabric/HIS push happens at the /commit boundary (sessions.py) alongside the rest of
    the flow's changes -- this only keeps the hot cache consistent so downstream reads
    see the reservation immediately."""
    synced = []
    for bed_id in inp.bed_ids:
        if not bed_id:
            continue
        existing = await cache.get(f"bed:{bed_id}") or {"id": bed_id}
        await cache.set_bed(bed_id, {**existing, "id": bed_id, "status": inp.status})
        synced.append(bed_id)
    if synced:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="bed_agent",
            event_type="bed_status_synced",
            payload={"bed_ids": synced, "status": inp.status},
        )
    logger.info("sync_bed_status  session=%s  synced=%d  status=%s", inp.session_id, len(synced), inp.status)
    return {"synced": bool(synced), "bed_ids": synced}


@activity.defn
async def hold_bed_temporarily(inp: HoldBedInput) -> dict:
    """Soft-hold a bed in Redis for a patient en route (TTL 15 min, no approval needed)."""
    key = f"bed_hold:{inp.bed_id}"
    await cache.set(key, {"session_id": inp.session_id, "patient_token": inp.patient_token}, ttl=900)
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "info",
        "message": f"Bed {inp.bed_id} soft-held for 15 minutes for patient en route.",
    })
    logger.info("hold_bed_temporarily  session=%s  bed=%s", inp.session_id, inp.bed_id)
    return {"held": True, "bed_id": inp.bed_id}


# -- sa_dirty_bed_recovery ------------------------------------------------------------

@dataclass
class EmergencyCleaningInput:
    session_id: str
    dirty_beds: list = field(default_factory=list)


@activity.defn
async def create_emergency_cleaning_task(inp: EmergencyCleaningInput) -> dict:
    """Create a retrievable emergency-cleaning task record per dirty bed (Redis +
    audit), not just an audit row. Each task is keyed cleaning_task:{id} (24h TTL) with
    an emergency priority and a 10-minute SLA so housekeeping/ops can look it up."""
    tasks = []
    for b in inp.dirty_beds:
        bed_id = b.get("id")
        if not bed_id:
            continue
        task_id = str(uuid.uuid4())
        record = {
            "task_id": task_id,
            "bed_id": bed_id,
            "ward": b.get("ward"),
            "bed_number": b.get("bed_number"),
            "priority": "emergency",
            "sla_minutes": 10,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": inp.session_id,
        }
        await cache.set(f"cleaning_task:{task_id}", record, ttl=86400)
        tasks.append(record)

    if tasks:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="bed_agent",
            event_type="emergency_cleaning_tasks_created",
            payload={"task_ids": [t["task_id"] for t in tasks], "bed_ids": [t["bed_id"] for t in tasks]},
        )
    logger.info("create_emergency_cleaning_task  session=%s  created=%d", inp.session_id, len(tasks))
    return {"task_ids": [t["task_id"] for t in tasks], "tasks": tasks, "created": len(tasks)}


@activity.defn
async def dispatch_housekeeping_fast_track(inp: EmergencyCleaningInput) -> dict:
    """Broadcast a high-priority fast-track alert for each dirty bed."""
    beds = inp.dirty_beds
    for bed in beds:
        await broadcast(inp.session_id, {
            "type":     "alert",
            "severity": "critical",
            "message":  f"[FAST-TRACK] {bed.get('ward', 'Unknown')} -- Bed {bed.get('bed_number', bed.get('id', '')[:8])}: emergency housekeeping required (SLA < 10 min).",
        })

    if beds:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="bed_agent",
            event_type="fast_track_cleaning_dispatched",
            payload={"beds": [{"id": b.get("id"), "ward": b.get("ward")} for b in beds]},
        )

    logger.info("dispatch_housekeeping_fast_track  session=%s  dispatched=%d", inp.session_id, len(beds))
    return {"dispatched": len(beds)}


@activity.defn
async def escalate_to_floor_supervisor(inp: NotifyInput) -> dict:
    """Escalate a cleaning delay to the floor supervisor via broadcast."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "warning",
        "message": inp.message or "Cleaning delay escalated to floor supervisor.",
        **inp.payload,
    })
    logger.info("escalate_to_floor_supervisor  session=%s", inp.session_id)
    return {"escalated": True}


def _bed_id_of(b) -> str | None:
    """Accept either a bed record dict or a bare bed id."""
    return (b.get("id") if isinstance(b, dict) else b) or None


@activity.defn
async def validate_sanitization(inp: BedReadinessInput) -> dict:
    """Validate sanitization from Redis bed status (no sensor/checklist feed exists): a
    bed passes once it is no longer Dirty/Cleaning. Beds still mid-clean are reported as
    pending rather than silently failing the whole batch."""
    passed_ids, pending_ids = [], []
    for b in inp.beds:
        bed_id = _bed_id_of(b)
        if not bed_id:
            continue
        rec = await cache.get(f"bed:{bed_id}") or (b if isinstance(b, dict) else {})
        if (rec.get("status") or "").lower() in ("dirty", "cleaning"):
            pending_ids.append(bed_id)
        else:
            passed_ids.append(bed_id)
    passed = bool(passed_ids) and not pending_ids
    logger.info("validate_sanitization  session=%s  passed=%d  pending=%d", inp.session_id, len(passed_ids), len(pending_ids))
    return {"passed": passed, "passed_ids": passed_ids, "pending_ids": pending_ids, "basis": "redis_status"}


@activity.defn
async def mark_bed_ready(inp: BedReadinessInput) -> dict:
    """Mark cleaned beds clean & available in the hot cache, protect them from poller
    overwrite, stage them for the /commit HIS push, and audit. No direct Fabric call --
    /commit (sessions.py) propagates the Available status to HIS with rollback
    protection alongside the rest of the flow's changes."""
    ready_ids = []
    for b in inp.beds:
        bed_id = _bed_id_of(b)
        if not bed_id:
            continue
        existing = await cache.get(f"bed:{bed_id}") or (b if isinstance(b, dict) else {"id": bed_id})
        await cache.set_bed(bed_id, {**existing, "id": bed_id, "status": "Available"})
        await cache.mark_bed_freed(bed_id)
        ready_ids.append(bed_id)

    if ready_ids:
        prior = await cache.get_staged(inp.session_id, "cleaned_beds") or []
        merged = list(dict.fromkeys([*prior, *ready_ids]))
        await cache.stage(inp.session_id, "cleaned_beds", merged)
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="bed_agent",
            event_type="beds_marked_ready",
            payload={"bed_ids": ready_ids},
        )
    logger.info("mark_bed_ready  session=%s  ready=%d", inp.session_id, len(ready_ids))
    return {"bed_ids": ready_ids, "bed_id": ready_ids[0] if ready_ids else None}


@activity.defn
async def check_room_readiness(inp: BedReadinessInput) -> dict:
    """Aggregate room readiness from Redis bed records (no sensor feed): a room is ready
    when its bed is active and not Dirty/Cleaning. Concrete blocking issues are surfaced
    per bed instead of an opaque False."""
    issues, ready_ids = [], []
    for b in inp.beds:
        bed_id = _bed_id_of(b)
        if not bed_id:
            continue
        rec = await cache.get(f"bed:{bed_id}") or (b if isinstance(b, dict) else {})
        status = (rec.get("status") or "").lower()
        if not rec.get("is_active", True):
            issues.append({"bed_id": bed_id, "issue": "bed_inactive"})
        elif status in ("dirty", "cleaning"):
            issues.append({"bed_id": bed_id, "issue": f"awaiting_cleaning ({status})"})
        else:
            ready_ids.append(bed_id)
    ready = bool(ready_ids) and not issues
    logger.info("check_room_readiness  session=%s  ready=%d  issues=%d", inp.session_id, len(ready_ids), len(issues))
    return {"ready": ready, "ready_ids": ready_ids, "issues": issues}


@activity.defn
async def validate_oxygen_readiness(inp: BedReadinessInput) -> dict:
    """Derive O2-pipeline readiness from Redis bed features (no telemetry feed exists): a
    bed is functional when it advertises oxygen/ventilation capability. Beds with no O2
    field are reported unknown, never assumed functional."""
    functional_ids, unknown_ids = [], []
    for b in inp.beds:
        bed_id = _bed_id_of(b)
        if not bed_id:
            continue
        rec = await cache.get(f"bed:{bed_id}") or (b if isinstance(b, dict) else {})
        if rec.get("ventilation") or _has_feature(rec, "oxygen") or _has_feature(rec, "ventilator") or _has_feature(rec, "o2"):
            functional_ids.append(bed_id)
        else:
            unknown_ids.append(bed_id)
    functional = bool(functional_ids) and not unknown_ids
    logger.info("validate_oxygen_readiness  session=%s  functional=%d  unknown=%d", inp.session_id, len(functional_ids), len(unknown_ids))
    return {"functional": functional, "functional_ids": functional_ids, "unknown_ids": unknown_ids, "basis": "redis_features"}


@activity.defn
async def check_monitor_readiness(inp: BedReadinessInput) -> dict:
    """Derive bedside-monitor readiness from Redis bed features (no telemetry feed
    exists). Beds with no monitor feature are reported unknown, never assumed functional."""
    functional_ids, unknown_ids = [], []
    for b in inp.beds:
        bed_id = _bed_id_of(b)
        if not bed_id:
            continue
        rec = await cache.get(f"bed:{bed_id}") or (b if isinstance(b, dict) else {})
        if _has_feature(rec, "monitor") or _has_feature(rec, "cardiac_monitor") or _has_feature(rec, "telemetry"):
            functional_ids.append(bed_id)
        else:
            unknown_ids.append(bed_id)
    functional = bool(functional_ids) and not unknown_ids
    logger.info("check_monitor_readiness  session=%s  functional=%d  unknown=%d", inp.session_id, len(functional_ids), len(unknown_ids))
    return {"functional": functional, "functional_ids": functional_ids, "unknown_ids": unknown_ids, "basis": "redis_features"}


@activity.defn
async def notify_biomedical_team(inp: NotifyInput) -> dict:
    """Alert biomedical engineering for equipment fault via broadcast."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "warning",
        "message": inp.message or "Equipment fault -- biomedical engineering alerted.",
        **inp.payload,
    })
    logger.info("notify_biomedical_team  session=%s", inp.session_id)
    return {"notified": True}


@activity.defn
async def sync_ready_status(inp: BedReadinessInput) -> dict:
    """Hand off ready beds to the Bed Assignment flow: stage the ready set in Redis and
    broadcast so downstream consumers can pick them up."""
    ready_ids = [bid for bid in (_bed_id_of(b) for b in inp.beds) if bid]
    if ready_ids:
        await cache.stage(inp.session_id, "ready_beds", ready_ids)
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "info",
            "message": f"{len(ready_ids)} bed(s) cleaned and ready for assignment.",
        })
    logger.info("sync_ready_status  session=%s  ready=%d", inp.session_id, len(ready_ids))
    return {"synced": bool(ready_ids), "bed_ids": ready_ids}


@activity.defn
async def create_equipment_task(inp: NotifyInput) -> dict:
    """Stub -- create equipment setup/repair task (no task management integration yet)."""
    logger.warning("create_equipment_task  session=%s  STUB", inp.session_id)
    return {"task_id": None}


# -- sa_bed_prediction ----------------------------------------------------------

@activity.defn
async def predict_icu_saturation(inp: PredictSaturationInput) -> dict:
    """Derive ICU saturation risk from Redis bed census."""
    all_beds = await cache.get_all_beds()
    icu_beds = [b for b in all_beds if "ICU" in (b.get("ward") or "").upper() and b.get("is_active", True)]
    if not icu_beds:
        return {"saturation_pct": 0, "risk": "unknown"}
    occupied = [b for b in icu_beds if b.get("status") in ("reserved", "occupied")]
    saturation_pct = round(len(occupied) / len(icu_beds) * 100)
    risk = "high" if saturation_pct >= 90 else "medium" if saturation_pct >= 75 else "low"
    logger.info("predict_icu_saturation  session=%s  saturation=%d%%  risk=%s", inp.session_id, saturation_pct, risk)
    return {"saturation_pct": saturation_pct, "risk": risk}


@activity.defn
async def generate_capacity_alert(inp: NotifyInput) -> dict:
    """Generate an alert when predicted occupancy exceeds threshold."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "warning",
        "message": inp.message or "Capacity threshold exceeded.",
        **inp.payload,
    })
    logger.info("generate_capacity_alert  session=%s", inp.session_id)
    return {"alert_sent": True}


@activity.defn
async def trigger_surge_forecast(session_id: str) -> dict:
    """Stub -- surge forecast trigger (no inflow time-series data yet)."""
    logger.warning("trigger_surge_forecast  session=%s  STUB", session_id)
    return {"forecast": None}


@activity.defn
async def recommend_overflow_strategy(inp: NotifyInput) -> dict:
    """Stub -- Claude reasoning over census for redistribution plan (use bed_prediction_workflow instead)."""
    logger.warning("recommend_overflow_strategy  session=%s  STUB -- delegate to bed_prediction_workflow", inp.session_id)
    return {"strategy": None}


@activity.defn
async def predict_discharge_probability(session_id: str) -> dict:
    """Probability each admitted ICU patient discharges soon. Reuses the discharge
    agent's clinical readiness logic (services.discharge.assess_discharge) and maps the
    outcome to a probability bucket. Capped to bound cost; truncation is logged.

    STUB -- reused the discharge agent's readiness logic, which is not part of
    this 5-domain slice (bed/ICU/staff/ER/revenue)."""
    logger.warning("predict_discharge_probability  session=%s  STUB -- discharge agent not in this slice", session_id)
    return {"predictions": [], "truncated": False}


@activity.defn
async def notify_discharge_team(inp: NotifyInput) -> dict:
    """Notify discharge team of high-probability discharge patients."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "info",
        "message": inp.message or "Discharge team notified of high-probability patients.",
        **inp.payload,
    })
    logger.info("notify_discharge_team  session=%s", inp.session_id)
    return {"notified": True}


@activity.defn
async def trigger_clearance_workflow(session_id: str) -> dict:
    """Stub -- trigger billing/pharmacy clearance for discharge (no clearance workflow yet)."""
    logger.warning("trigger_clearance_workflow  session=%s  STUB", session_id)
    return {"triggered": False}


@activity.defn
async def predict_discharge_horizon(session_id: str) -> dict:
    """Forecast time-to-next-discharge for bed planning using the Fabric discharge
    horizon counts. horizon_minutes is the soonest window with an expected discharge
    (0 if patients are already discharge-ready). Degrades gracefully if Fabric is down."""
    try:
        ready_now   = await hasura.get_discharge_ready_count()
        freeing_4h  = await hasura.get_discharge_horizon(4)
        freeing_24h = await hasura.get_discharge_horizon(24)
    except Exception as exc:
        logger.warning("predict_discharge_horizon  session=%s  fabric unavailable: %s", session_id, exc)
        return {"horizon_minutes": None, "freeing_4h": None, "freeing_24h": None, "error": "fabric_unavailable"}

    if ready_now and ready_now > 0:
        horizon = 0
    elif freeing_4h and freeing_4h > 0:
        horizon = 240
    elif freeing_24h and freeing_24h > 0:
        horizon = 1440
    else:
        horizon = None
    logger.info("predict_discharge_horizon  session=%s  horizon=%s  ready_now=%s  4h=%s  24h=%s",
                session_id, horizon, ready_now, freeing_4h, freeing_24h)
    return {
        "horizon_minutes": horizon,
        "discharge_ready_now": ready_now,
        "freeing_4h": freeing_4h,
        "freeing_24h": freeing_24h,
    }


@activity.defn
async def run_surge_model(session_id: str) -> dict:
    """Stub -- ER admission surge demand model (no inflow data yet)."""
    logger.warning("run_surge_model  session=%s  STUB", session_id)
    return {"demand_forecast": None}


@activity.defn
async def alert_operations_team(inp: NotifyInput) -> dict:
    """Alert operations team of surge prediction via broadcast."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "warning",
        "message": inp.message or "Operations team alerted -- surge predicted.",
        **inp.payload,
    })
    logger.info("alert_operations_team  session=%s", inp.session_id)
    return {"notified": True}


@activity.defn
async def notify_staffing_agent(session_id: str) -> dict:
    """Stub -- notify staffing agent of predicted surge demand (no cross-agent messaging yet)."""
    logger.warning("notify_staffing_agent  session=%s  STUB", session_id)
    return {"notified": False}


@activity.defn
async def recommend_overflow_zone(session_id: str) -> dict:
    """Stub -- recommend temporary expansion/overflow zone (no zone mapping yet)."""
    logger.warning("recommend_overflow_zone  session=%s  STUB", session_id)
    return {"recommendation": None}


# -- sa_discharge_coordination --------------------------------------------------

@activity.defn
async def trigger_discharge_coordination(session_id: str) -> dict:
    """Signal discharge workflow to accelerate discharge via broadcast alert."""
    await broadcast(session_id, {
        "type": "alert",
        "severity": "info",
        "message": "Discharge coordination triggered -- accelerating pending discharges.",
    })
    await hasura.write_audit(
        session_id=session_id,
        agent_id="bed_agent",
        event_type="discharge_coordination_triggered",
        payload={},
    )
    logger.info("trigger_discharge_coordination  session=%s", session_id)
    return {"triggered": True}


# -- sa_escalation --------------------------------------------------------------

@activity.defn
async def escalate_to_command_center(inp: NotifyInput) -> dict:
    """Broadcast a full escalation alert to the command center."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "critical",
        "message": inp.message or "Escalation: command center notified.",
        **inp.payload,
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="bed_agent",
        event_type="command_center_escalation",
        payload=inp.payload,
    )
    logger.info("escalate_to_command_center  session=%s", inp.session_id)
    return {"escalated": True}


@activity.defn
async def escalate_allocation_conflict(inp: NotifyInput) -> dict:
    """Escalate when no compliant bed can be found for a patient."""
    await broadcast(inp.session_id, {
        "type": "alert",
        "severity": "critical",
        "message": inp.message or "Allocation conflict -- no compliant bed available. Manual review required.",
        **inp.payload,
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="bed_agent",
        event_type="allocation_conflict_escalated",
        payload=inp.payload,
    )
    logger.info("escalate_allocation_conflict  session=%s", inp.session_id)
    return {"escalated": True}
