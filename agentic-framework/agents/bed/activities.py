import asyncio
import json
import logging
from dataclasses import dataclass, field

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from agents._shared.ranking import rank_beds
from workflows.temporal.workflow._escalation import start_escalating_approval
from api.routes.ws import broadcast
from util.idem import make_idem_key

logger = logging.getLogger(__name__)


def _patient_token(patient: dict) -> str | None:
    """Extract patient token from either 'patient_token' or 'token' field."""
    return patient.get("patient_token") or patient.get("token") or None


def _patient_ctx(raw) -> dict:
    """Normalise the session_patient:{sid} value to a single context dict.

    The key now holds a LIST of patient contexts (single-patient = list of 1).
    These single-reservation activities act on one patient, so take the first.
    Tolerates the legacy single-dict shape and a JSON string."""
    if not raw:
        return {}
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if isinstance(raw, list):
        return raw[0] if raw and isinstance(raw[0], dict) else {}
    return raw if isinstance(raw, dict) else {}


def _name_display(token: str | None, patient_map: dict) -> tuple[str, str]:
    """Returns (full_name, display_id) for a patient token."""
    if not token:
        return "Unknown Patient", "--"
    p = patient_map.get(token)
    if not p:
        return f"Patient {token[:8]}", token[:8]
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"Patient {token[:8]}"
    uid  = p.get("uhid") or token[:8]
    return name, uid


@dataclass
class RankBedsInput:
    session_id: str
    candidates: list
    patient_context: dict = field(default_factory=dict)


@dataclass
class BedApprovalInput:
    session_id: str
    bed_id: str


@dataclass
class ReleaseLockInput:
    session_id: str
    bed_id: str


@dataclass
class BatchAssignmentInput:
    session_id: str
    critical_patients: list   # [{visit_id, triage_score, bed_type_needed, vitals, ...}]
    available_beds: list


@activity.defn
async def find_available_beds(session_id: str) -> list:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_availability"})
    all_beds = await cache.get_all_beds()
    candidates = [
        b for b in all_beds
        if b.get("status") == "Available" and b.get("is_active", True)
    ]
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_availability",
        "result": {"candidate_count": len(candidates)},
    })
    logger.info("bed availability  session=%s  candidates=%d", session_id, len(candidates))
    return candidates


@activity.defn
async def rank_beds_activity(inp: RankBedsInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_ranking"})

    # Use patient_context passed directly; fall back to Redis session key
    patient_context: dict = inp.patient_context or {}
    if not patient_context:
        patient_context = _patient_ctx(await cache.get(f"session_patient:{inp.session_id}"))

    anon = {
        "acuity":              patient_context.get("acuity") or patient_context.get("triage_score"),
        "required_bed_type":   patient_context.get("required_bed_type") or patient_context.get("bed_type_needed"),
        "isolation_required":  patient_context.get("isolation_required", False),
        "current_unit":        patient_context.get("current_unit"),
        "chief_complaint":     patient_context.get("chief_complaint"),
        "vitals":              patient_context.get("vitals"),
    }
    ranking = await rank_beds(anon, inp.candidates)

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_ranking",
        "result": {"recommendation": ranking.get("recommendation")},
    })
    return ranking


@activity.defn
async def find_beds_for_patients(inp: BatchAssignmentInput) -> list:
    """
    For each critical patient, pick the best matching bed from the available pool.
    Filters by bed_type_needed (ICU / HDU / General), removes each selected bed
    from the pool so two patients don't get assigned the same bed.
    Returns list of {patient, bed_id, bed} assignments.
    """
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_batch_match"})

    all_beds = list(inp.available_beds)
    assignments = []

    def suitable_beds(patient: dict) -> list:
        needed = patient.get("bed_type_needed", "General")
        def matches(b: dict) -> bool:
            ward = (b.get("ward") or b.get("type") or "").upper()
            if needed == "ICU":
                return "ICU" in ward
            if needed == "HDU":
                return "HDU" in ward or "HIGH" in ward
            return "ICU" not in ward and "HDU" not in ward and "HIGH" not in ward
        filtered = [b for b in all_beds if matches(b)]
        return filtered or all_beds

    async def rank_for_patient(patient: dict) -> tuple:
        needed   = patient.get("bed_type_needed", "General")
        suitable = suitable_beds(patient)
        ctx      = {
            "triage_score":    patient.get("triage_score"),
            "bed_type_needed": needed,
            "chief_complaint": patient.get("chief_complaint"),
            "vitals":          patient.get("vitals"),
        }
        ranking = await rank_beds(ctx, suitable[:20])
        return patient, suitable, ranking

    results = await asyncio.gather(
        *[rank_for_patient(p) for p in inp.critical_patients],
        return_exceptions=True,
    )

    used_ids: set = set()
    for item in results:
        if isinstance(item, Exception):
            logger.error("ranking failed for a patient: %s", item)
            continue
        patient, suitable, ranking = item
        ranked = ranking.get("ranked_beds", [])
        bed_id = next(
            (r["bed_id"] for r in ranked if r["bed_id"] not in used_ids),
            None,
        )
        if bed_id is None:
            bed_id = next(
                (b.get("id") or b.get("bed_id") for b in suitable
                 if (b.get("id") or b.get("bed_id")) not in used_ids),
                None,
            )
        if bed_id is None:
            logger.warning("No bed available for patient %s", patient.get("visit_id"))
            continue
        used_ids.add(bed_id)
        chosen = next((b for b in suitable if (b.get("id") or b.get("bed_id")) == bed_id), suitable[0])
        assignments.append({"patient": patient, "bed_id": bed_id, "bed": chosen})

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_batch_match",
        "result": {"assigned": len(assignments), "requested": len(inp.critical_patients)},
    })
    logger.info("batch bed match  session=%s  assigned=%d", inp.session_id, len(assignments))
    return assignments


@activity.defn
async def create_batch_bed_approval(session_id: str, assignments: list) -> str:
    """Lock all selected beds in Redis and create a single batch approval task."""
    for a in assignments:
        lock_key = f"bed_lock:{a['bed_id']}"
        await cache.acquire_bed_lock(lock_key, session_id)

    tokens = [t for a in assignments if (t := _patient_token(a.get("patient") or {}))]
    patient_map = await hasura.get_patient_names(tokens)

    def _bed_label(a: dict) -> str:
        bed = a.get("bed") or {}
        ward    = bed.get("ward") or bed.get("type") or ""
        bed_num = bed.get("bed_number") or a["bed_id"]
        return f"{ward} – {bed_num}" if ward else bed_num

    enriched = []
    for a in assignments:
        token = _patient_token(a.get("patient") or {})
        name, uid = _name_display(token, patient_map)
        enriched.append({**a, "patient_name": name, "patient_id": uid})

    summary_lines = [
        f"{_bed_label(a)} -> CTAS {a['patient'].get('triage_score')} "
        f"({a['patient'].get('bed_type_needed')}) -- {a['patient'].get('chief_complaint', 'unknown')}"
        for a in enriched
    ]
    approval = await hasura.create_approval_task(
        session_id=session_id,
        agent_id="bed_agent",
        action_type="bed_reservation",
        payload={"assignments": enriched, "summary": summary_lines},
        idempotency_key=make_idem_key(
            "bed_reservation", session_id, sorted(a["bed_id"] for a in assignments)),
    )
    approval_id = approval["id"]

    await broadcast(session_id, {
        "type": "approval_required",
        "approval_id": approval_id,
        "action": "bed_reservation",
        "bed_count": len(enriched),
        "summary": summary_lines,
        "assignments": enriched,
    })
    logger.info("batch approval created  session=%s  approval=%s  beds=%d",
                session_id, approval_id, len(assignments))
    await start_escalating_approval(
        session_id=session_id,
        approval_id=approval_id,
        agent_id="bed_agent",
        action_type="bed_reservation",
        payload={"assignments": enriched, "summary": summary_lines},
    )
    return approval_id


@activity.defn
async def confirm_batch_reservations(session_id: str, assignments: list) -> dict:
    """Confirm all bed reservations in Redis and write audit log."""
    for a in assignments:
        bed_id  = a["bed_id"]
        patient = a["patient"]
        # patch, not set: a partial set_bed would drop ward/bed_number and hide the bed
        # from the ICU filters until the next change event restored it.
        await cache.patch_bed(bed_id, {"status": "reserved", "session_id": session_id})
        await hasura.write_audit(
            session_id=session_id,
            agent_id="bed_agent",
            event_type="bed_reserved",
            payload={"bed_id": bed_id, "patient_token": patient.get("patient_token"), "triage_score": patient.get("triage_score")},
            idempotency_key=make_idem_key("audit_bed_reserved", session_id, bed_id),
        )

    bed_ids = [a["bed_id"] for a in assignments]
    bed_names = [
        f"{(a.get('bed') or {}).get('ward', '')} – {(a.get('bed') or {}).get('bed_number', a['bed_id'])}".strip(" –")
        for a in assignments
    ]
    icu_beds_reserved = sum(1 for a in assignments if "ICU" in (a.get("bed") or {}).get("ward", "").upper())
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_reservation",
        "result": {"beds_reserved": len(assignments), "bed_ids": bed_ids, "bed_names": bed_names},
    })
    logger.info("batch reservations confirmed  session=%s  beds=%s", session_id, bed_ids)
    return {
        "beds_reserved":     len(assignments),
        "icu_beds_reserved": icu_beds_reserved,
        "bed_ids":           bed_ids,
        "bed_names":         bed_names,
        "status":            "confirmed",
    }


@activity.defn
async def release_batch_locks(session_id: str, assignments: list) -> None:
    for a in assignments:
        await cache.release_bed_lock(f"bed_lock:{a['bed_id']}", session_id)
    logger.info("batch locks released  session=%s  beds=%d", session_id, len(assignments))


@activity.defn
async def create_bed_approval(inp: BedApprovalInput) -> dict:
    lock_key = f"bed_lock:{inp.bed_id}"
    acquired = await cache.acquire_bed_lock(lock_key, inp.session_id)
    if not acquired:
        raise RuntimeError(f"Bed {inp.bed_id} already locked by another session")

    patient_context = _patient_ctx(await cache.get(f"session_patient:{inp.session_id}"))
    patient_token = patient_context.get("token", "UNKNOWN")

    # Look up bed details so the approval modal shows real ward/bed names
    bed_data = await cache.get(f"bed:{inp.bed_id}") or {}
    if not isinstance(bed_data, dict):
        bed_data = {}
    ward       = bed_data.get("ward", "")
    bed_number = bed_data.get("bed_number", "")

    token_val = patient_token if patient_token != "UNKNOWN" else None
    patient_map = await hasura.get_patient_names([token_val] if token_val else [])
    patient_name, patient_id = _name_display(token_val, patient_map)

    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="bed_agent",
        action_type="bed_reservation",
        payload={"bed_id": inp.bed_id, "patient_token": patient_token},
        idempotency_key=make_idem_key("bed_reservation", inp.session_id, inp.bed_id),
    )
    approval_id = approval["id"]

    assignments = [{
        "bed_id":       inp.bed_id,
        "bed":          {"ward": ward, "bed_number": bed_number},
        "patient":      patient_context,
        "patient_name": patient_name,
        "patient_id":   patient_id,
    }]

    await broadcast(inp.session_id, {
        "type":        "approval_required",
        "approval_id": approval_id,
        "action":      "bed_reservation",
        "bed_id":      inp.bed_id,
        "assignments": assignments,
    })
    logger.info("approval created  session=%s  approval=%s  bed=%s  ward=%s",
                inp.session_id, approval_id, inp.bed_id, ward or "(unknown)")
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval_id,
        agent_id="bed_agent",
        action_type="bed_reservation",
        payload={"bed_id": inp.bed_id, "patient_token": patient_token},
    )
    return {"approval_id": approval_id}


@activity.defn
async def confirm_bed_reservation(inp: BedApprovalInput) -> dict:
    patient_context = _patient_ctx(await cache.get(f"session_patient:{inp.session_id}"))
    patient_token = patient_context.get("token", "UNKNOWN")

    # patch, not set: a partial set_bed would drop ward/bed_number and hide the bed from
    # the ICU filters until the next change event restored it. patch_bed returns the
    # merged record, so ward/bed_number are read back from that rather than snapshotted
    # before an overwrite.
    merged = await cache.patch_bed(inp.bed_id, {
        "status": "reserved",
        "session_id": inp.session_id,
    })
    ward       = merged.get("ward") or ""
    bed_number = merged.get("bed_number") or ""
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="bed_agent",
        event_type="bed_reserved",
        payload={"bed_id": inp.bed_id, "patient_token": patient_token},
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_reservation",
        "result": {"bed_id": inp.bed_id, "status": "reserved", "ward": ward, "bed_number": bed_number},
    })
    logger.info("bed reserved  session=%s  bed=%s", inp.session_id, inp.bed_id)
    return {"bed_id": inp.bed_id, "status": "confirmed"}


@activity.defn
async def release_bed_lock_activity(inp: ReleaseLockInput) -> None:
    lock_key = f"bed_lock:{inp.bed_id}"
    await cache.release_bed_lock(lock_key, inp.session_id)
    logger.info("bed lock released  session=%s  bed=%s", inp.session_id, inp.bed_id)
