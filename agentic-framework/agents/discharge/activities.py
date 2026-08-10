import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from temporalio import activity

from db.hasura import hasura
from cache import redis as cache
from util.idem import make_idem_key
from workflows.temporal.workflow._escalation import start_escalating_approval
from fhirgw import repository as repo
from fhirgw.mappers import observation as obs_map
from agents.discharge.service import assess_discharge, generate_discharge_summary
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _task_dict(t) -> dict:
    """Flatten a FHIR Task to the {task, task_type, due_at} shape the discharge
    service + note/lab keyword filters expect."""
    code = getattr(t, "code", None)
    period = getattr(t, "executionPeriod", None)
    start = getattr(period, "start", None) if period else None
    return {
        "task":      t.description,
        "task_type": (code.text if code else None) or t.description,
        "due_at":    start.isoformat() if hasattr(start, "isoformat") else start,
    }


async def _pending_task_dicts(admission_id: str) -> list[dict]:
    return [_task_dict(t) for t in await repo.nursing_tasks_for(admission_id)]


async def _latest_vitals(patient_token: str) -> dict | None:
    return await hasura.get_latest_vitals(patient_token) if patient_token else None


@dataclass
class BatchAssessInput:
    session_id: str
    admissions: list  # list of dicts from get_discharge_eligible_admissions


@dataclass
class DischargeApprovalInput:
    session_id: str
    ready_ids: list      # admission IDs Claude marked ready
    all_results: list    # full assessment list for the approval payload


@dataclass
class DischargeConfirmInput:
    session_id: str
    assessments: list    # list of {admission_id, discharge_ready, blocked_reason}


@dataclass
class SummaryGenInput:
    session_id: str
    ready_admissions: list   # admissions where discharge_ready=True


@dataclass
class NotesCheckInput:
    session_id: str
    ready_admissions: list


@activity.defn
async def get_discharge_candidates(session_id: str) -> list:
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_discharge_candidates",
    })
    all_admissions = await cache.get_all_admissions()
    admissions = [a for a in all_admissions if (a.get("status") or "").lower() == "admitted"]
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_discharge_candidates",
        "result": {"candidate_count": len(admissions)},
    })
    logger.info("discharge candidates  session=%s  count=%d", session_id, len(admissions))
    return admissions


@activity.defn
async def get_discharge_records(session_id: str) -> list:
    """G21: retrospective path — fetch closed/discharged encounters instead of active admissions."""
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_discharge_retrospective",
    })
    all_admissions = await cache.get_all_admissions()
    records = [a for a in all_admissions if (a.get("status") or "").lower() in ("discharged", "closed")]
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_discharge_retrospective",
        "result": {"record_count": len(records)},
    })
    logger.info("discharge records  session=%s  count=%d", session_id, len(records))
    return records


@activity.defn
async def batch_assess_discharges(inp: BatchAssessInput) -> list:
    """
    For each admission: fetch pending tasks + latest vitals + summary presence,
    then call Claude to produce a discharge readiness assessment.
    Returns list of {admission_id, discharge_ready, blocked_reason, assessment}.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_discharge_assessment",
    })

    async def _assess_one(admission: dict) -> dict:
        admission_id  = admission["id"]
        patient_token = admission.get("patient_token", "")
        tasks         = await _pending_task_dicts(admission_id)          # FHIR Task -> dicts
        vitals        = admission.get("vitals") or await _latest_vitals(patient_token)
        assessment    = await assess_discharge(admission, tasks, vitals)
        return {
            "admission_id":    admission_id,
            "bed_id":          admission.get("bed_id"),
            "discharge_ready": assessment.get("discharge_ready", False),
            "blocked_reason":  assessment.get("blocked_reason"),
            "assessment":      assessment.get("assessment", ""),
        }

    raw = await asyncio.gather(*[_assess_one(a) for a in inp.admissions], return_exceptions=True)
    results = []
    for admission, outcome in zip(inp.admissions, raw):
        if isinstance(outcome, Exception):
            logger.error("discharge assessment failed  admission=%s  error=%s",
                         admission["id"][:8], outcome)
            results.append({
                "admission_id":    admission["id"],
                "discharge_ready": False,
                "blocked_reason":  "assessment_error",
                "assessment":      "Assessment unavailable due to API error",
            })
        else:
            results.append(outcome)

    ready_count   = sum(1 for r in results if r["discharge_ready"])
    blocked_count = len(results) - ready_count

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_discharge_assessment",
        "result": {
            "assessed":      len(results),
            "ready":         ready_count,
            "blocked":       blocked_count,
        },
    })
    logger.info(
        "discharge batch  session=%s  assessed=%d  ready=%d  blocked=%d",
        inp.session_id, len(results), ready_count, blocked_count,
    )
    return results


@activity.defn
async def create_discharge_approval(inp: DischargeApprovalInput) -> dict:
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="discharge_agent",
        action_type="mark_discharge_ready",
        payload={
            "ready_count":  len(inp.ready_ids),
            "ready_ids":    inp.ready_ids,
            "all_results":  inp.all_results,
        },
        idempotency_key=make_idem_key(
            "mark_discharge_ready", inp.session_id, sorted(inp.ready_ids)),
    )
    await broadcast(inp.session_id, {
        "type": "approval_required",
        "approval_id": approval["id"],
        "action": "mark_discharge_ready",
        "ready_count": len(inp.ready_ids),
    })
    logger.info(
        "discharge approval created  session=%s  approval=%s  ready=%d",
        inp.session_id, approval["id"], len(inp.ready_ids),
    )
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="discharge_agent",
        action_type="mark_discharge_ready",
        payload={"ready_count": len(inp.ready_ids), "ready_ids": inp.ready_ids,
                 "all_results": inp.all_results},
    )
    return {"approval_id": approval["id"]}


@activity.defn
async def confirm_discharge_updates(inp: DischargeConfirmInput) -> dict:
    """Stage confirmed discharge status updates for commit."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_discharge_confirm"})
    staged = [
        {
            "admission_id":    item["admission_id"],
            "discharge_ready": item["discharge_ready"],
            "blocked_reason":  item.get("blocked_reason"),
            "bed_id":          item.get("bed_id"),
        }
        for item in inp.assessments
    ]
    await cache.stage(inp.session_id, "discharge", staged)

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="discharge_agent",
        event_type="discharge_status_staged",
        payload={"count": len(staged)},
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_discharge_confirm",
        "result": {"confirmed": len(staged)},
    })
    logger.info("discharge staged  session=%s  count=%d", inp.session_id, len(staged))
    return {"confirmed": len(staged)}


@activity.defn
async def generate_discharge_summaries(inp: SummaryGenInput) -> dict:
    """Generate AI discharge notes for each discharge-ready patient that lacks one."""
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_discharge_summary",
    })

    generated = 0
    for admission in inp.ready_admissions:
        admission_id  = admission["id"]
        patient_token = admission.get("patient_token", "")

        vitals = obs_map.vitals_to_internal(await repo.latest_vitals(patient_token)) or None
        completed_count = await repo.completed_task_count(admission_id)

        note = await generate_discharge_summary(admission, vitals, completed_count)
        await hasura.set_ai_discharge_note(admission_id, note)
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="discharge_agent",
            event_type="ai_discharge_note_generated",
            payload={"admission_id": admission_id, "chars": len(note)},
        )
        generated += 1

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_discharge_summary",
        "result": {"summaries_generated": generated},
    })
    logger.info("discharge summaries generated  session=%s  count=%d",
                inp.session_id, generated)
    return {"summaries_generated": generated}


@activity.defn
async def check_notes_completeness(inp: NotesCheckInput) -> dict:
    """Check whether all clinical notes are present for discharge-ready patients."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_notes_check"})
    incomplete = []
    for admission in inp.ready_admissions:
        admission_id = admission["id"]
        pending_tasks = await _pending_task_dicts(admission_id)
        note_tasks = [t for t in pending_tasks if "note" in (t.get("task_type") or "").lower()
                      or "document" in (t.get("task_type") or "").lower()
                      or "summary" in (t.get("task_type") or "").lower()]
        if note_tasks:
            incomplete.append({"admission_id": admission_id, "missing_tasks": len(note_tasks)})
    result = {"notes_incomplete": len(incomplete), "incomplete_admissions": incomplete}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_notes_check", "result": {"notes_incomplete": len(incomplete)}})
    logger.info("check_notes_completeness  session=%s  incomplete=%d", inp.session_id, len(incomplete))
    return result


@activity.defn
async def request_missing_docs(inp: NotesCheckInput) -> dict:
    """Broadcast documentation requests for admissions with missing notes."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_missing_docs"})
    for admission in inp.ready_admissions:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Missing documentation for admission {admission['admission_id'][:8]} -- discharge summary on hold.",
            "admission_id": admission["admission_id"],
        })
    if inp.ready_admissions:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="discharge_agent",
            event_type="missing_docs_requested",
            payload={"count": len(inp.ready_admissions), "admission_ids": [a["admission_id"] for a in inp.ready_admissions]},
        )
    result = {"requested": len(inp.ready_admissions)}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_missing_docs", "result": result})
    logger.info("request_missing_docs  session=%s  requested=%d", inp.session_id, len(inp.ready_admissions))
    return result


@activity.defn
async def check_pending_results(inp: NotesCheckInput) -> dict:
    """Check for pending lab or imaging results before finalizing discharge summary."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_pending_results"})
    pending_admissions = []
    for admission in inp.ready_admissions:
        pending = await _pending_task_dicts(admission["id"])
        lab_pending = [t for t in pending if any(
            kw in (t.get("task_type") or "").lower()
            for kw in ("lab", "imaging", "radiology", "pathology", "scan", "xray", "mri")
        )]
        if lab_pending:
            pending_admissions.append({"admission_id": admission["id"], "pending_count": len(lab_pending)})
    if pending_admissions:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(pending_admissions)} patient(s) have pending lab/imaging results -- discharge summary held.",
        })
    result = {"results_pending": len(pending_admissions), "admissions_with_pending": pending_admissions}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_pending_results", "result": {"results_pending": len(pending_admissions)}})
    logger.info("check_pending_results  session=%s  pending=%d", inp.session_id, len(pending_admissions))
    return result


# -- sa_discharge_volume -------------------------------------------------------

# -- sa_weekend_discharge ------------------------------------------------------

# -- sa_discharge_delayed ------------------------------------------------------
