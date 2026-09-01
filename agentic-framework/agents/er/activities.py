import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from temporalio import activity

from fhir.resources.encounter import Encounter

from cache import redis as cache
from db.hasura import hasura
from fhirgw import repository as repo
from fhirgw.mappers._common import ref_id, reason_text, bare_id
from agents._shared.vitals_bulk import bulk_vitals_observations, tokens_from_encounters
from agents.er.service import triage_er_visits
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _fhir_json(resource) -> dict:
    return resource.model_dump(mode="json", by_alias=True, exclude_none=True)


def _enc_arrived(enc: Encounter):
    """actualPeriod.start as a value (datetime or str), or None."""
    period = getattr(enc, "actualPeriod", None)
    return getattr(period, "start", None) if period else None


def _flat_visit(enc: Encounter) -> dict:
    """Small dict view of an EMER Encounter for the downstream activities."""
    arrived = _enc_arrived(enc)
    return {
        "id":              enc.id,
        "patient_token":   ref_id(enc.subject),
        "chief_complaint": reason_text(enc.reason),
        "arrived_at":      arrived.isoformat() if hasattr(arrived, "isoformat") else arrived,
        "status":          enc.status,
    }


@dataclass
class ErTriageInput:
    session_id: str
    visits: list


@dataclass
class ErSaveInput:
    session_id: str
    triage_results: list   # [{visit_id, score, reason, needs_vitals}]


@dataclass
class ErFasttrackInput:
    session_id: str
    triage_results: list   # scored triage results from save_triage_scores


@dataclass
class SelectCriticalInput:
    session_id: str
    triage_results: list
    n: int = 5


@activity.defn
async def get_er_visits(session_id: str) -> list:
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_er_census",
    })
    encounters = await repo.er_visits()                 # FHIR Encounter (EMER)
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_er_census",
        "result": {"active_er_count": len(encounters)},
    })
    logger.info("ER census  session=%s  active=%d", session_id, len(encounters))
    return [_fhir_json(e) for e in encounters]


@activity.defn
async def triage_er_patients(inp: ErTriageInput) -> list:
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_er_triage",
    })

    now = datetime.now(timezone.utc)
    # inp.visits are FHIR Encounter (EMER) JSON; re-parse and fetch vitals as FHIR Observations.
    encounters = [Encounter.model_validate(e) for e in inp.visits]
    # Vitals for the whole ER census in ONE Fabric call. This loop used to await
    # repo.latest_vitals() per visit, so triage cost one call per active ER
    # patient -- 65 serial calls on the reference dataset, and the single largest
    # contributor to a live flow's Fabric traffic.
    vitals_by_token = await bulk_vitals_observations(tokens_from_encounters(encounters))
    items = []
    for enc in encounters:
        token = ref_id(enc.subject)
        vitals = vitals_by_token.get(token, []) if token else []
        arrived = _enc_arrived(enc)
        wait_minutes = 0
        if arrived is not None:
            try:
                if isinstance(arrived, str):
                    arr = datetime.fromisoformat(arrived.replace("Z", "+00:00"))
                else:
                    arr = arrived
                if arr.tzinfo is None:
                    arr = arr.replace(tzinfo=timezone.utc)
                wait_minutes = int((now - arr).total_seconds() / 60)
            except Exception:
                pass
        items.append({"encounter": enc, "vitals": vitals, "wait_minutes": wait_minutes})

    triage = await triage_er_visits(items)

    # Re-attach patient_token + a flat visit view (from the Encounter) for downstream activities
    visit_by_id = {enc.id: _flat_visit(enc) for enc in encounters}
    for t in triage:
        original = visit_by_id.get(t.get("visit_id")) or {}
        t["patient_token"]   = original.get("patient_token")
        t["chief_complaint"]  = t.get("chief_complaint") or original.get("chief_complaint")
        t["visit"]            = original

    critical = [t for t in triage if t["score"] <= 2]
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_er_triage",
        "result": {
            "triaged":  len(triage),
            "critical": len(critical),
            "ctas_1":   sum(1 for t in triage if t["score"] == 1),
            "ctas_2":   sum(1 for t in triage if t["score"] == 2),
        },
    })
    return triage


@activity.defn
async def save_triage_scores(inp: ErSaveInput) -> dict:
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_er_save",
    })

    # bare_id strips any CarerOS prefix (`em-<uuid>`) so the Hospilot-owned write
    # keys on the bare visit uuid (no-op row if not present locally).
    scores = [{"visit_id": bare_id(t["visit_id"]), "score": t["score"]}
              for t in inp.triage_results]
    await hasura.bulk_set_triage_scores(scores)

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="er_agent",
        event_type="triage_scores_assigned",
        payload={"count": len(scores), "results": inp.triage_results},
    )

    critical = [t for t in inp.triage_results if t["score"] <= 2]
    if critical:
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "critical",
            "message": f"{len(critical)} CTAS 1-2 patient(s) require immediate attention",
            "patients": critical,
        })

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_er_save",
        "result": {"saved": len(scores), "critical_alerts": len(critical)},
    })
    logger.info("ER triage saved  session=%s  saved=%d  critical=%d",
                inp.session_id, len(scores), len(critical))
    return {"saved": len(scores), "critical": len(critical), "results": inp.triage_results}


@activity.defn
async def route_fasttrack_patients(inp: ErFasttrackInput) -> dict:
    """
    Identify CTAS 4-5 patients waiting >30 min and recommend fast-track / OPD diversion.
    No DB state change -- broadcasts the recommendation and writes an audit entry.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_er_fasttrack",
    })

    now = datetime.now(timezone.utc)
    fasttrack = []
    for t in inp.triage_results:
        if t.get("score") not in (4, 5):
            continue
        visit = t.get("visit") or {}
        arrived = visit.get("arrived_at") or t.get("arrived_at")
        wait_minutes = 0
        if arrived:
            try:
                arr = datetime.fromisoformat(arrived.replace("Z", "+00:00"))
                wait_minutes = int((now - arr).total_seconds() / 60)
            except Exception:
                pass
        if wait_minutes >= 30:
            fasttrack.append({
                "visit_id":    t.get("visit_id"),
                "score":       t.get("score"),
                "wait_minutes": wait_minutes,
                "complaint":   (visit.get("chief_complaint") or t.get("chief_complaint") or "unknown"),
                "action":      "OPD diversion" if t.get("score") == 5 else "fast-track queue",
            })

    if fasttrack:
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "info",
            "message": f"{len(fasttrack)} CTAS 4-5 patient(s) eligible for fast-track routing",
            "patients": fasttrack,
        })
        from db.hasura import hasura
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="er_agent",
            event_type="fasttrack_recommendations",
            payload={"count": len(fasttrack), "patients": fasttrack},
        )

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_er_fasttrack",
        "result": {"fasttrack_candidates": len(fasttrack)},
    })
    logger.info("ER fasttrack  session=%s  candidates=%d", inp.session_id, len(fasttrack))
    return {"fasttrack_candidates": len(fasttrack), "patients": fasttrack}


@activity.defn
async def select_critical_patients(inp: SelectCriticalInput) -> list:
    """
    Rank triage results by clinical severity (CTAS score + vitals) and return
    the top N patients with their bed_type_needed for downstream Bed Agent use.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_er_critical_select",
    })

    def severity_key(t: dict) -> tuple:
        score   = t.get("score", 5)
        vitals  = t.get("vitals") or {}
        spo2    = vitals.get("spo2") or 100
        pulse   = vitals.get("pulse") or 60
        # Lower CTAS = more urgent; within same CTAS, low SpO2 and high HR rank worse
        return (score, spo2, -pulse)

    # Only consider patients who need admission (CTAS 1-3)
    admission_candidates = [t for t in inp.triage_results if t.get("score", 5) <= 3]
    ranked = sorted(admission_candidates, key=severity_key)

    # Deduplicate by patient_token -- keep highest priority visit per patient
    seen_tokens: dict = {}
    deduped = []
    for t in ranked:
        token = (t.get("visit") or {}).get("patient_token") or t.get("patient_token")
        if token and token in seen_tokens:
            continue
        if token:
            seen_tokens[token] = True
        deduped.append(t)

    top_n = deduped[: inp.n]

    critical_patients = []
    for t in top_n:
        score  = t.get("score", 3)
        vitals = t.get("vitals") or {}
        visit  = t.get("visit") or {}

        if score == 1:
            bed_type = "ICU"
        elif score == 2:
            bed_type = "HDU"
        else:
            bed_type = "General"

        critical_patients.append({
            "visit_id":        t.get("visit_id"),
            "patient_token":   visit.get("patient_token") or t.get("patient_token"),
            "triage_score":    score,
            "chief_complaint": visit.get("chief_complaint") or t.get("chief_complaint", "unknown"),
            "vitals":          vitals,
            "bed_type_needed": bed_type,
            "triage_reason":   t.get("reason", ""),
        })

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_er_critical_select",
        "result": {"critical_selected": len(critical_patients)},
    })
    logger.info("ER critical select  session=%s  selected=%d", inp.session_id, len(critical_patients))
    return critical_patients


@activity.defn
async def detect_cardiac_arrest(inp: ErFasttrackInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_code_blue"})
    suspects = [t for t in inp.triage_results if t.get("cardiac_arrest_suspected")]
    for patient in suspects:
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "critical",
            "message": f"CODE BLUE: Cardiac arrest suspected -- visit {patient.get('visit_id')}",
            "patient": patient,
        })
    if suspects:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="er_agent",
            event_type="code_blue_triggered",
            payload={"count": len(suspects), "visit_ids": [p.get("visit_id") for p in suspects]},
        )
    result = {"cardiac_arrest_suspected": len(suspects), "code_blue_triggered": len(suspects) > 0}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_code_blue", "result": result})
    logger.info("detect_cardiac_arrest  session=%s  suspects=%d", inp.session_id, len(suspects))
    return result


@activity.defn
async def check_spo2_critical(inp: ErFasttrackInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_spo2_critical"})
    critical_patients = [t for t in inp.triage_results if t.get("spo2_critical")]
    for patient in critical_patients:
        vitals = patient.get("vitals") or {}
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "critical",
            "message": f"Critical SpO2: visit {patient.get('visit_id')} -- SpO2={vitals.get('spo2', 'unknown')}% -- escalating stabilization",
            "patient": patient,
        })
    if critical_patients:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="er_agent",
            event_type="spo2_escalation",
            payload={"count": len(critical_patients), "visit_ids": [p.get("visit_id") for p in critical_patients]},
        )
    result = {"spo2_critical": len(critical_patients), "escalated": len(critical_patients) > 0}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_spo2_critical", "result": result})
    logger.info("check_spo2_critical  session=%s  critical=%d", inp.session_id, len(critical_patients))
    return result


@activity.defn
async def detect_clinical_protocol(inp: ErFasttrackInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_protocol"})
    by_protocol: dict[str, list] = {}
    for t in inp.triage_results:
        proto = t.get("protocol", "none")
        if proto and proto != "none":
            by_protocol.setdefault(proto, []).append(t)
    for proto, patients in by_protocol.items():
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "critical",
            "message": f"{proto.upper()} PROTOCOL ACTIVATED: {len(patients)} patient(s)",
            "visit_ids": [p.get("visit_id") for p in patients],
        })
    total = sum(len(v) for v in by_protocol.values())
    if by_protocol:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="er_agent",
            event_type="clinical_protocol_activated",
            payload={"protocols": {k: [p.get("visit_id") for p in v] for k, v in by_protocol.items()}},
        )
    result = {
        "protocol_count":     total,
        "protocol_activated": total > 0,
        "protocols":          {k: [p.get("visit_id") for p in v] for k, v in by_protocol.items()},
    }
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_protocol", "result": result})
    logger.info("detect_clinical_protocol  session=%s  protocols=%s", inp.session_id, list(by_protocol.keys()))
    return result


@activity.defn
async def notify_specialist(inp: ErFasttrackInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_specialist"})
    patients_needing = [t for t in inp.triage_results if t.get("needs_specialist")]
    specialists: set[str] = set()
    for p in patients_needing:
        proto = p.get("protocol", "none")
        if proto == "stroke":
            specialists.add("Neurology")
        elif proto == "sepsis":
            specialists.add("Infectious Disease / Critical Care")
        elif proto == "trauma":
            specialists.add("Trauma Surgery")
        else:
            specialists.add("Internal Medicine / ICU")
    if patients_needing:
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "warning",
            "message": f"Specialist notification: {len(patients_needing)} patient(s) -- {', '.join(sorted(specialists))}",
            "visit_ids": [p.get("visit_id") for p in patients_needing],
        })
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="er_agent",
            event_type="specialist_notified",
            payload={"count": len(patients_needing), "specialists": sorted(specialists)},
        )
    result = {"notified": len(patients_needing), "specialists_notified": sorted(specialists)}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_specialist", "result": result})
    logger.info("notify_specialist  session=%s  notified=%d", inp.session_id, len(patients_needing))
    return result


@activity.defn
async def check_er_boarders(session_id: str) -> dict:
    """Check ER boarders (admitted patients still waiting in ER for a bed) and escalate SLA breaches."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_boarding"})
    all_visits = await cache.get_many("er_visit:*")
    boarders = [
        v for v in all_visits
        if isinstance(v, dict) and v.get("status") == "boarded"
    ]
    escalated = 0
    for b in boarders:
        wait_minutes = b.get("boarding_minutes") or b.get("wait_minutes") or 0
        if wait_minutes >= 60:
            await broadcast(session_id, {
                "type": "alert",
                "severity": "warning",
                "message": f"ER boarder SLA breach: visit {b.get('visit_id')} has been boarding {wait_minutes} min.",
            })
            escalated += 1
    result = {"boarders": len(boarders), "escalated": escalated}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_boarding", "result": result})
    logger.info("check_er_boarders  session=%s  boarders=%d  escalated=%d", session_id, len(boarders), escalated)
    return result
