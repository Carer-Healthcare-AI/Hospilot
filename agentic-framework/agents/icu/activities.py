import logging
from dataclasses import dataclass

from temporalio import activity

from fhir.resources.encounter import Encounter
from fhir.resources.location import Location

from db.hasura import hasura
from fhirgw import repository as repo
from fhirgw.mappers import observation as obs_map
from fhirgw.mappers._common import ref_id
from agents.icu.service import analyze_icu, rank_icu_admissions, is_critical
from util.idem import make_idem_key
from workflows.temporal.workflow._escalation import start_escalating_approval
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _fhir_json(resource) -> dict:
    """Canonical FHIR JSON for crossing the Temporal activity boundary."""
    return resource.model_dump(mode="json", by_alias=True, exclude_none=True)


def _enc_bed_id(enc: Encounter) -> str | None:
    """The referenced bed id from Encounter.location[0].location (or None)."""
    locs = getattr(enc, "location", None)
    if not locs:
        return None
    return ref_id(locs[0].location)


@dataclass
class IcuAnalysisInput:
    session_id: str
    icu_admissions: list             # FHIR Encounter JSON
    non_icu_admissions: list         # FHIR Encounter JSON
    available_beds: list             # FHIR Location JSON
    bed_by_id: dict | None = None    # bed_id -> FHIR Location JSON (occupied beds)


@dataclass
class IcuApprovalInput:
    session_id: str
    step_down_candidates: list
    escalation_candidates: list
    summary: str


@dataclass
class IcuConfirmInput:
    session_id: str
    critical_vital_ids: list   # vitals to flag is_critical=true
    assessments: list          # full Claude result for audit


@dataclass
class IcuTransferInput:
    session_id: str
    incoming_patients: list    # ER critical patients with bed_type_needed == "ICU"
    available_beds: list


@dataclass
class IcuAdmissionInput:
    session_id: str
    ranked_requests: list      # output of rank_icu_requests


@activity.defn
async def get_icu_census(session_id: str) -> dict:
    """Fetch ICU beds, current ICU patients, and non-ICU admitted patients."""
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_icu_census",
    })

    # FHIR-native reads: canonical Encounter / Location resources via the repository.
    icu_encs, icu_beds = await repo.icu_admissions()
    non_encs, non_beds = await repo.non_icu_admissions()
    available_beds     = await repo.available_icu_beds()
    bed_by_id          = {**icu_beds, **non_beds}   # occupied beds, for ventilation lookup

    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_census",
        "result": {
            "icu_occupied":      len(icu_encs),
            "icu_available":     len(available_beds),
            "non_icu_admitted":  len(non_encs),
        },
    })
    logger.info(
        "ICU census  session=%s  icu=%d  available=%d  ward=%d",
        session_id, len(icu_encs), len(available_beds), len(non_encs),
    )
    return {
        "icu_admissions":     [_fhir_json(e) for e in icu_encs],
        "non_icu_admissions": [_fhir_json(e) for e in non_encs],
        "available_beds":     [_fhir_json(b) for b in available_beds],
        "bed_by_id":          {k: _fhir_json(v) for k, v in bed_by_id.items()},
    }


@activity.defn
async def analyze_icu_status(inp: IcuAnalysisInput) -> dict:
    """
    Fetch vitals for all patients, pre-filter escalation candidates by critical thresholds,
    then call Claude for step-down and escalation recommendations.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_icu_analysis",
    })

    # Re-parse the FHIR JSON handed across the activity boundary into models.
    icu_encs  = [Encounter.model_validate(a) for a in inp.icu_admissions]
    non_encs  = [Encounter.model_validate(a) for a in inp.non_icu_admissions]
    beds      = [Location.model_validate(b) for b in inp.available_beds]
    bed_by_id = {k: Location.model_validate(v) for k, v in (inp.bed_by_id or {}).items()}

    def _bed_for(enc: Encounter):
        bid = _enc_bed_id(enc)
        return bed_by_id.get(bid) if bid else None

    # Fetch vitals (FHIR Observations) for ICU patients
    icu_with_vitals = []
    for enc in icu_encs:
        token  = ref_id(enc.subject)
        vitals = await repo.latest_vitals(token) if token else []
        icu_with_vitals.append({"encounter": enc, "vitals": vitals, "bed": _bed_for(enc)})

    # Fetch vitals for non-ICU patients and keep only those with critical readings.
    # Deduplicate by patient token -- same patient across multiple admissions appears once.
    escalation_candidates = []
    critical_vital_ids: list[str] = []
    seen_tokens: set[str] = set()
    for enc in non_encs:
        token  = ref_id(enc.subject)
        vitals = await repo.latest_vitals(token) if token else []
        if vitals and is_critical(vitals):
            if token and token in seen_tokens:
                continue
            if token:
                seen_tokens.add(token)
            escalation_candidates.append({"encounter": enc, "vitals": vitals, "bed": _bed_for(enc)})
            vid = obs_map.vitals_to_internal(vitals).get("id")
            if vid:
                critical_vital_ids.append(vid)

    result = await analyze_icu(icu_with_vitals, escalation_candidates, beds)
    result["critical_vital_ids"] = critical_vital_ids
    # Combined count gating ta_create_icu_approval: the approval fires when there is at
    # least one transfer candidate of EITHER kind (step-down OR escalation). Typed
    # conditions are single-symbol with no OR, so the union is surfaced as one field.
    result["transfer_candidate_count"] = (
        len(result.get("step_down_candidates", [])) + len(result.get("escalation_candidates", []))
    )
    result["vitals_by_admission_id"] = {
        p["encounter"].id: obs_map.vitals_to_internal(p["vitals"])
        for p in icu_with_vitals + escalation_candidates
        if p.get("vitals")
    }

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_analysis",
        "result": {
            "step_down":    len(result.get("step_down_candidates", [])),
            "escalations":  len(result.get("escalation_candidates", [])),
            "critical_vitals": len(critical_vital_ids),
        },
    })
    logger.info(
        "ICU analysis  session=%s  step_down=%d  escalations=%d  critical_vitals=%d",
        inp.session_id,
        len(result.get("step_down_candidates", [])),
        len(result.get("escalation_candidates", [])),
        len(critical_vital_ids),
    )
    return result


@activity.defn
async def create_icu_approval(inp: IcuApprovalInput) -> dict:
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="icu_agent",
        action_type="icu_transfer_recommendations",
        payload={
            "step_down_candidates":   inp.step_down_candidates,
            "escalation_candidates":  inp.escalation_candidates,
            "summary":                inp.summary,
        },
        idempotency_key=make_idem_key(
            "icu_transfer", inp.session_id,
            sorted(c.get("admission_id") for c in
                   (inp.step_down_candidates + inp.escalation_candidates))),
    )

    # Fetch patient names -- patients are not cached in Redis on this branch, use Fabric directly.
    all_tokens = list({
        c["patient_token"]
        for c in inp.step_down_candidates + inp.escalation_candidates
        if c.get("patient_token")
    })
    patient_map = await hasura.get_patient_names(all_tokens)

    def _patient_display(token: str | None) -> tuple[str, str]:
        """Returns (full_name, display_id) for a patient token."""
        if not token:
            return "Unknown Patient", "--"
        p = patient_map.get(token)
        if not p:
            return f"Patient {token[:8]}", token[:8]
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"Patient {token[:8]}"
        uid  = p.get("uhid") or token[:8]
        return name, uid

    step_downs = []
    for c in inp.step_down_candidates:
        name, uid = _patient_display(c.get("patient_token"))
        step_downs.append({
            "patient_name": name,
            "patient_id":   uid,
            "reason":       c.get("reason") or c.get("chief_complaint") or "Step-down recommended",
            "confidence":   c.get("confidence", ""),
        })

    escalations = []
    for c in inp.escalation_candidates:
        name, uid = _patient_display(c.get("patient_token"))
        escalations.append({
            "patient_name": name,
            "patient_id":   uid,
            "reason":       c.get("reason") or c.get("chief_complaint") or "ICU escalation needed",
            "urgency":      c.get("urgency", ""),
        })

    await broadcast(inp.session_id, {
        "type":             "approval_required",
        "approval_id":      approval["id"],
        "action":           "icu_transfer_recommendations",
        "step_down_count":  len(inp.step_down_candidates),
        "escalation_count": len(inp.escalation_candidates),
        "summary":          inp.summary,
        "step_downs":       step_downs,
        "escalations":      escalations,
    })
    logger.info(
        "ICU approval created  session=%s  approval=%s  step_down=%d  escalations=%d",
        inp.session_id, approval["id"],
        len(inp.step_down_candidates), len(inp.escalation_candidates),
    )
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="icu_agent",
        action_type="icu_transfer_recommendations",
        payload={"step_down_candidates": inp.step_down_candidates,
                 "escalation_candidates": inp.escalation_candidates, "summary": inp.summary},
    )
    return {"approval_id": approval["id"]}


@activity.defn
async def confirm_icu_actions(inp: IcuConfirmInput) -> dict:
    from cache import redis as cache

    # Flag critical vitals immediately -- safety-critical, no staging
    flagged = 0
    for vital_id in inp.critical_vital_ids:
        await repo.mark_observation_critical(vital_id)
        flagged += 1

    # Stage transfer_pending update for /commit
    assessments = inp.assessments if isinstance(inp.assessments, dict) else {}
    transfer_admission_ids = [
        c["admission_id"]
        for c in (
            assessments.get("step_down_candidates", []) +
            assessments.get("escalation_candidates", [])
        )
        if c.get("admission_id")
    ]
    await cache.stage(inp.session_id, "icu", {"transfer_admission_ids": transfer_admission_ids})

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_recommendations_staged",
        payload=inp.assessments,
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_confirm",
        "result": {
            "critical_vitals_flagged": flagged,
            "transfers_staged": len(transfer_admission_ids),
        },
    })
    logger.info("ICU staged  session=%s  flagged=%d  transfers_staged=%d",
                inp.session_id, flagged, len(transfer_admission_ids))
    return {"critical_vitals_flagged": flagged, "transfers_staged": len(transfer_admission_ids)}


# -- sa_icu_transfer ------------------------------------------------------------

@activity.defn
async def rank_icu_requests(inp: IcuTransferInput) -> dict:
    """Rank incoming ICU admission requests by clinical acuity."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_transfer"})
    beds = [Location.model_validate(b) for b in inp.available_beds]
    result = await rank_icu_admissions(inp.incoming_patients, beds)
    ranked = result.get("ranked_requests", [])
    out = {
        "ranked_requests":          ranked,
        "ventilator_dependent_count": sum(1 for r in ranked if r.get("ventilator_dependent")),
        "deterioration_risk_count":   sum(1 for r in ranked if r.get("deterioration_risk_high")),
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_icu_transfer",
        "result": {k: v for k, v in out.items() if k != "ranked_requests"},
    })
    logger.info("rank_icu_requests  session=%s  ranked=%d", inp.session_id, len(ranked))
    return out


@activity.defn
async def prioritize_ventilator_bed(inp: IcuAdmissionInput) -> dict:
    """Reorder ranked requests so ventilator-dependent patients are first."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_ventilator_priority"})
    vent = [r for r in inp.ranked_requests if r.get("ventilator_dependent")]
    non_vent = [r for r in inp.ranked_requests if not r.get("ventilator_dependent")]
    reordered = vent + non_vent
    if vent:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Ventilator ICU bed prioritized: {len(vent)} ventilator-dependent patient(s)",
            "patients": [r.get("patient_token") for r in vent],
        })
    result = {"ventilator_priority_count": len(vent), "ranked_requests": reordered}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_ventilator_priority", "result": {"ventilator_priority_count": len(vent)}})
    logger.info("prioritize_ventilator_bed  session=%s  ventilator_patients=%d", inp.session_id, len(vent))
    return result


@activity.defn
async def reserve_icu_admission(inp: IcuAdmissionInput) -> dict:
    """Create an ICU admission approval for the top-ranked patient."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_reserve"})
    if not inp.ranked_requests:
        return {"approval_id": None, "patient_token": None}
    top = inp.ranked_requests[0]
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="icu_agent",
        action_type="icu_admission_request",
        payload={
            "patient_token": top.get("patient_token"),
            "rank":          top.get("rank", 1),
            "reason":        top.get("reason", ""),
            "ventilator_dependent": top.get("ventilator_dependent", False),
        },
        idempotency_key=make_idem_key(
            "icu_admission", inp.session_id, top.get("patient_token")),
    )
    await broadcast(inp.session_id, {
        "type": "approval_required",
        "approval_id": approval["id"],
        "action": "icu_admission_request",
        "patient_token": top.get("patient_token"),
        "reason": top.get("reason", ""),
        "ventilator_dependent": top.get("ventilator_dependent", False),
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_admission_reserved",
        payload={"patient_token": top.get("patient_token"), "approval_id": approval["id"]},
    )
    result = {"approval_id": approval["id"], "patient_token": top.get("patient_token")}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_reserve", "result": result})
    logger.info("reserve_icu_admission  session=%s  patient=%s", inp.session_id, top.get("patient_token"))
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="icu_agent",
        action_type="icu_admission_request",
        payload={"patient_token": top.get("patient_token"), "rank": top.get("rank", 1),
                 "reason": top.get("reason", ""), "ventilator_dependent": top.get("ventilator_dependent", False)},
    )
    return result


@activity.defn
async def trigger_overflow_evaluation(inp: IcuAdmissionInput) -> dict:
    """Broadcast overflow alert when ICU is full and admission requests are pending."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_overflow"})
    pending = len(inp.ranked_requests)
    await broadcast(inp.session_id, {
        "type": "alert", "severity": "critical",
        "message": f"ICU at full capacity -- {pending} admission request(s) pending. Overflow evaluation triggered.",
        "patients": [r.get("patient_token") for r in inp.ranked_requests],
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_overflow_evaluation_triggered",
        payload={"patients_pending": pending},
    )
    result = {"overflow_triggered": True, "patients_pending": pending}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_overflow", "result": result})
    logger.info("trigger_overflow_evaluation  session=%s  pending=%d", inp.session_id, pending)
    return result


@activity.defn
async def escalate_deterioration(inp: IcuAdmissionInput) -> dict:
    """Escalate high-deterioration-risk patients to immediate attention."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_deterioration"})
    high_risk = [r for r in inp.ranked_requests if r.get("deterioration_risk_high")]
    for patient in high_risk:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "critical",
            "message": f"HIGH DETERIORATION RISK: patient {patient.get('patient_token', 'unknown')} -- {patient.get('reason', '')}",
            "patient_token": patient.get("patient_token"),
        })
    if high_risk:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="icu_agent",
            event_type="deterioration_escalated",
            payload={"count": len(high_risk), "patients": [r.get("patient_token") for r in high_risk]},
        )
    result = {"escalated": len(high_risk)}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_deterioration", "result": result})
    logger.info("escalate_deterioration  session=%s  escalated=%d", inp.session_id, len(high_risk))
    return result

