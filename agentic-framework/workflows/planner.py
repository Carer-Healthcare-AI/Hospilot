import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity

from llm_client import llm_chat
from db.hasura import hasura
from workflows.graph.prefetch import PREFETCH_TASK_RUNNERS
from workflows.strategies import (
    strategy_catalogue_text, is_valid_strategy,
)
from schemas.types import SubAgent, Task

logger = logging.getLogger("planner")

_PROMPT_DIR = Path(__file__).parent / "system_prompts"

def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _dump_prompt(label: str, system, user: str) -> None:
    """DEBUG: print the EXACT final prompt (system + built user message) passed to
    the model, clearly labelled. Goes to stdout so it can be captured separately
    from logging (which writes to stderr). Remove once prompt inspection is done."""
    #if isinstance(system, str):
    #    sys_text = system
    #elif isinstance(system, list):
    #    sys_text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
    #else:
    #    sys_text = str(system)
    #print("\n" + "#" * 100)
    #print(f"### PROMPT DUMP :: {label}")
    #print("#" * 100)
    #print("----------------------------- SYSTEM PROMPT -----------------------------")
    #print(sys_text)
    #print("------------------------------ USER PROMPT ------------------------------")
    #print(user)
    #print("#" * 100 + "\n", flush=True)


def _json_object(text: str) -> str:
    """Carve the outermost JSON object out of an LLM response.

    Models fence their JSON, and -- markedly more often when the prompt asks them to
    REVISE an existing plan rather than build one -- prefix it with a sentence or two
    of reasoning ("Looking at the feedback: … the structure remains identical, only …").
    Stripping fences alone leaves that preamble in place and json.loads dies on char 0.
    Take the span from the first '{' to the last '}' so both shapes survive; the caller
    still does the json.loads (and its own fallback if the span isn't valid JSON).
    Same carve-out `critique_pipeline` already does inline.
    """
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


# -- Sub-agent task planner ----------------------------------------------------

@dataclass
class PlanSubagentInput:
    agent_id:        str
    subagent_id:     str
    available_tasks: list           # [{"id": ..., "label": ..., "outputs": [...]}]
    ta_results:      dict = field(default_factory=dict)
    goal:            str  = ""
    session_id:      str  = ""
    subgoal:         str  = ""       # what THIS sub-agent must achieve (from stage-2 selection)


_SUBAGENT_SYSTEM = _load_prompt("subagent_tasks.txt")

_SUBAGENT_USER = """Goal: {goal}

Sub-agent: {subagent_id}
Sub-agent goal: {subgoal}

Prior task results:
{ta_results}

Available tasks (id -> label -> outputs):
{task_catalogue}

Return only the tasks to execute:
{{
  "<task_id>": {{"condition": null}},
  "<task_id>": {{"condition": {{"symbol": "<task_id>.<field>", "op": ">", "value": 0}}}}
}}"""


@activity.defn
async def plan_subagent_tasks(inp: PlanSubagentInput) -> dict:
    """
    Dynamically select and condition tasks for one sub-agent.
    Called at the START of each sub-agent's execution block, after prior tasks have run.
    Returns {task_id: {"condition": null | {symbol, op, value}}} -- ready to drop into task_plan.
    """
    # If caller didn't pass task catalog, fetch from DB
    if not inp.available_tasks:
        rows = await hasura.fetch_agent_registry()
        _, db_sub_agents, _, _ = _build_registry_from_rows(rows)
        sa = next(
            (s for s in db_sub_agents.get(inp.agent_id, []) if s.id == inp.subagent_id),
            None,
        )
        inp = PlanSubagentInput(
            agent_id=inp.agent_id,
            subagent_id=inp.subagent_id,
            available_tasks=[{"id": t.id, "label": t.label, "outputs": t.outputs} for t in sa.tasks] if sa else [],
            ta_results=inp.ta_results,
            goal=inp.goal,
            session_id=inp.session_id,
        )

    # Always merge in user-added dynamic tasks (may not be in the hardcoded list)
    try:
        dynamic = await hasura.fetch_dynamic_tasks(inp.subagent_id)
        if dynamic:
            existing_ids = {t["id"] for t in inp.available_tasks}
            new_tasks = [
                {"id": t["id"], "label": t["label"], "outputs": t.get("outputs") or []}
                for t in dynamic if t["id"] not in existing_ids
            ]
            if new_tasks:
                inp.available_tasks = list(inp.available_tasks) + new_tasks
                logger.debug("merged %d dynamic task(s)  subagent=%s", len(new_tasks), inp.subagent_id)
    except Exception as exc:
        logger.warning("dynamic task merge failed  subagent=%s  err=%s", inp.subagent_id, exc)

    # Strip large list fields from ta_results so the prompt stays tight
    ta_summary = {
        k: {fk: fv for fk, fv in v.items() if fk not in ("candidates", "dirty_beds", "beds", "visits", "triage")}
        for k, v in inp.ta_results.items()
    } if inp.ta_results else {}

    task_catalogue = "\n".join(
        f"  {t['id']}: {t['label']}  outputs={t.get('outputs', [])}"
        for t in inp.available_tasks
    )

    _user_content = _SUBAGENT_USER.format(
        goal=inp.goal or "(not specified)",
        subagent_id=inp.subagent_id,
        subgoal=inp.subgoal or "(not specified)",
        ta_results=json.dumps(ta_summary, indent=2) if ta_summary else "none",
        task_catalogue=task_catalogue,
    )
    _dump_prompt(
        f"TASKS-LEVEL BUILD / REORCHESTRATE TASKS INSIDE SUBAGENT  "
        f"agent={inp.agent_id}  subagent={inp.subagent_id}",
        _SUBAGENT_SYSTEM, _user_content,
    )
    try:
        text = await llm_chat(
            system=_SUBAGENT_SYSTEM,
            user=_user_content,
            max_tokens=1024,
            temperature=0.0,  # deterministic: same goal -> same task selection
            tier="fast",
        )
    except Exception:
        logger.exception("plan_subagent_tasks failed  subagent=%s -- running all tasks", inp.subagent_id)
        return {t["id"]: {"condition": None, "label": t.get("label", t["id"]), "outputs": t.get("outputs", [])} for t in inp.available_tasks}

    text = _json_object(text)

    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("plan_subagent_tasks bad JSON  subagent=%s -- running all tasks", inp.subagent_id)
        return {t["id"]: {"condition": None, "label": t.get("label", t["id"]), "outputs": t.get("outputs", [])} for t in inp.available_tasks}

    selected = list(plan.keys())
    logger.info(
        "subagent plan  subagent=%-30s  selected=%s",
        inp.subagent_id, selected,
    )
    # Enrich each entry with task label/outputs so run_dynamic_tasks can use them
    task_meta = {t["id"]: t for t in inp.available_tasks}
    return {
        task_id: {
            **entry,
            "label":   task_meta.get(task_id, {}).get("label", task_id),
            "outputs": task_meta.get(task_id, {}).get("outputs", []),
        }
        for task_id, entry in plan.items()
    }

_SELECT_SUBAGENTS_SYSTEM = _load_prompt("select_subagents.txt")

_SELECT_SUBAGENTS_USER = """Goal: {goal}

Pipeline context -- the agent graph and the other agents by proximity
(DO NOT select IDs from here; for reasoning only):
{pipeline_context}

Reorchestrating agent: {agent_id}

Available sub-agents for {agent_id} (select ONLY from this list):
{subagent_list}

Return the sub-agents to run, in execution order, each with a one-line subgoal.
Optionally include a "condition" string when a sub-agent must ONLY run if a specific
runtime state holds (e.g. "icu_full", "icu_not_full", "icu_available == 0").
Omit "condition" when the sub-agent should always run if selected.
{{"selected": [{{"id": "sa_...", "subgoal": "what this sub-agent must achieve here", "condition": "optional_condition_or_omit"}}, ...]}}"""


# G44: Step-Down Coordinator and ICU Admissions REPORTING must run regardless of
# ICU fullness -- the step-down candidates and the admissions/overflow queue are
# needed most precisely when the ICU is full. A blanket fullness gate on these
# sub-agents withholds the info exactly then. The action tasks inside them are
# already gated individually (reserve on icu_available>0, overflow on
# icu_available==0, transfer approval on transfer_candidate_count>0), so dropping
# the sub-agent-level fullness condition is safe.
_ICU_REPORTING_SUBAGENTS = {"sa_icu_stepdown", "sa_icu_transfer"}
_ICU_FULLNESS_CONDS = {"icu_full", "icu_not_full"}


async def select_subagents(
    agent_id: str, subagents: list, goal: str, pipeline_context: str = "",
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Select which sub-agents should run for an agent given the goal.

    Returns ``(ordered_ids, subgoals, conditions)`` where ``subgoals`` maps
    sa_id -> one-line subgoal and ``conditions`` maps sa_id -> condition string
    (only present when the LLM attached a runtime condition to that sub-agent).
    On failure, falls back to all sub-agents with empty subgoals/conditions."""
    def _sa_line(sa) -> str:
        task_names = ", ".join(t.id for t in sa.tasks[:4])
        desc = (getattr(sa, "description", "") or "").strip()
        line = f"- {sa.id}: {sa.label}" + (f" -- {desc}" if desc else "")
        return line + (f" (tasks: {task_names})" if task_names else "")
    subagent_list = "\n".join(_sa_line(sa) for sa in subagents)
    _user_content = _SELECT_SUBAGENTS_USER.format(
        agent_id=agent_id,
        goal=goal,
        subagent_list=subagent_list,
        pipeline_context=pipeline_context or "  (none)",
    )
    _dump_prompt(
        f"SUBAGENTS-LEVEL BUILD / REORCHESTRATE SUBAGENTS IN AGENT  agent={agent_id}",
        _SELECT_SUBAGENTS_SYSTEM, _user_content,
    )
    try:
        text = await llm_chat(
            system=_SELECT_SUBAGENTS_SYSTEM,
            user=_user_content,
            max_tokens=1024,
            temperature=0.0,  # deterministic: same goal -> same sub-agent selection
            tier="fast",
        )
        text = _json_object(text)
        result = json.loads(text)
        valid_ids = {sa.id for sa in subagents}
        selected: list[str] = []
        subgoals: dict[str, str] = {}
        conditions: dict[str, str] = {}
        for item in result.get("selected", []):
            # Accept both the new {"id","subgoal","condition"} shape and a bare "sa_id" string.
            sid = item.get("id") if isinstance(item, dict) else item
            if sid in valid_ids and sid not in selected:
                selected.append(sid)
                if isinstance(item, dict):
                    if item.get("subgoal"):
                        subgoals[sid] = item["subgoal"].strip()
                    cond = (item.get("condition") or "").strip()
                    if cond and cond != "optional_condition_or_omit":
                        if sid in _ICU_REPORTING_SUBAGENTS and cond.lower() in _ICU_FULLNESS_CONDS:
                            # G44: reporting must always run -- drop the fullness gate.
                            logger.info("G44: dropping fullness gate %r from %s", cond, sid)
                        else:
                            conditions[sid] = cond
        if not selected:
            return [sa.id for sa in subagents], {}, {}
        return selected, subgoals, conditions
    except Exception:
        logger.exception("select_subagents failed  agent=%s -- keeping all", agent_id)
        return [sa.id for sa in subagents], {}, {}


# Hardcoded fallback used ONLY when the DB registry fetch fails. Descriptions
# mirror the agent_registry.description column in hospilot_app -- they feed the
# AGENT PURPOSES section of the agent-graph prompt (see _build_system_prompt).
AVAILABLE_AGENTS = [
    {"id": "bed_agent",            "label": "Bed Management",      "color": "#3b82f6",
     "description": "owns finding/reserving a specific bed for a specific patient NOW; when ICU is full it also recovers dirty ICU beds (emergency cleaning -> mark ready)"},
    {"id": "icu_agent",            "label": "ICU Operations",      "color": "#8b5cf6",
     "description": "owns ICU census, capacity, step-down (ICU->ward transfer; INTERNAL, patient stays in hospital), critical-patient escalation, escalation deferral"},
    {"id": "er_agent",             "label": "ER Coordination",     "color": "#ef4444",
     "description": "owns EMERGENCY DEPARTMENT flow for patients ARRIVING at the ED from outside the hospital: triage/CTAS scoring, acuity response, fast-track, admission selection, and ER boarding (patients admitted FROM the ED still awaiting an inpatient bed). Scope is NEW ED arrivals only. Do NOT use for in-hospital events involving patients who never came through the ED: an already-admitted inpatient's fall or deterioration on a ward is a clinical-escalation event, not ER triage; and elective/scheduled or ward patients waiting for an inpatient bed belong to bed_agent -- ER triage/CTAS/boarding logic does not apply to them."},
    {"id": "staff_agent",          "label": "Staffing",            "color": "#f59e0b",
     "description": "owns nurse-to-patient ratios, shift coverage, float-pool deployment"},
    {"id": "discharge_agent",      "label": "Discharge Planning",  "color": "#10b981",
     "description": "owns getting patients OUT of the hospital entirely (home/SNF). NEVER use for ICU step-down -- step-down is icu_agent's. discharge_agent only applies when patients are leaving the hospital, not moving between units."},
    {"id": "pharmacy_agent",        "label": "Pharmacy",            "color": "#06b6d4",
     "description": "owns drug stock, dispensing queue, STAT orders, interactions, substitutions"},
    {"id": "lab_agent",             "label": "Lab Operations",      "color": "#06b6d4",
     "description": "owns lab order tracking, sample management, TAT, critical-result escalation, QC"},
    {"id": "ot_agent",             "label": "OT Scheduling",       "color": "#7c3aed",
     "description": "owns LIVE operating-theatre scheduling: today's/upcoming surgical list, theatre capacity & turnaround, emergency case insertion, and post-op bed planning -- always read from the live/forward OT schedule. MAY run alongside financial agents when live surgical scheduling is genuinely part of the goal (e.g. an overrunning list plus its cost impact, or confirming pre-auth before booking tomorrow's cases). The one thing it does NOT do is a RETROSPECTIVE look-back: a 'what happened' audit of PAST surgical cases or denied claims (TPA / pre-auth / denial audit over closed encounters) reads historical records owned by revenue_agent or billing_agent -- do not add ot_agent for a look-back audit, because its live schedule holds none of that history."},
    {"id": "revenue_agent",        "label": "Revenue",             "color": "#f97316",
     "description": "PREDICT & PREVENT revenue loss: hospital-wide billing-gap & leakage review, package/department profitability, resource utilization, AND insurance denial-risk PREDICTION & PREVENTION (pre-submission validation, payer rules, missing docs). Include when the goal is explicitly financial/analytical (billing review, leakage/profitability analysis, denial-risk review, financial health, daily/shift briefing, performance review). Do NOT add to acute clinical goals (triage, ICU, bed placement, staffing, pharmacy) -- it returns empty noise. Does NOT create bills or look up a single patient's invoices -- that is billing_agent."},
    {"id": "billing_agent",        "label": "Billing",             "color": "#0ea5e9",
     "description": "EXECUTE billing operations: structural claim validation (discrepancies, eligibility, compliance), collections / payment recovery, single-patient invoice & claim LOOKUP, and BILL GENERATION. Include for explicit claims-quality / compliance / collection goals, OR to CREATE/RAISE/GENERATE a bill, OR to look up one named patient's invoices/claims. task_type \"initiate_billing\" to create a bill for a resolved patient (lead with patient_verification_agent); \"patient_billing\" for a read-only invoice/claim lookup of one named patient; omit task_type for hospital-wide claims/collections review."},
    {"id": "ambulance_agent",      "label": "Ambulance Dispatch",  "color": "#ef4444",
     "description": "owns ambulance dispatch / pre-arrival coordination (patient NOT in the building yet)"},
    {"id": "patient_verification_agent", "label": "Patient Verification", "color": "#14b8a6",
     "description": "owns establishing patient IDENTITY (mobile -> patient_token + vitals). An INPUT PREREQUISITE that runs FIRST whenever the goal acts on ONE specific patient -- incoming (ER arrival, ICU admit, bed reservation) OR a specifically-named ADMITTED patient (discharge / billing-audit THE one named patient). NOT the goal owner. Omit for ward-wide/batch goals (discharge sweep, capacity, hospital-wide billing)."},
]


# -- Registry helpers ---------------------------------------------------------
# Agents, sub-agents, and tasks are stored in the DB (hospilot_agent_registry,
# hospilot_subagent_registry, hospilot_task_registry).
# These helpers convert the flat DB rows into the dicts the planner logic needs.

def _build_registry_from_rows(rows: list[dict]) -> tuple[list[dict], dict, dict, dict]:
    """
    Convert DB rows from hasura.fetch_agent_registry() into four working dicts:
      available_agents         -- [{"id", "label", "color"}] for the agent catalogue
      sub_agents               -- {agent_id: [SubAgent(...)]}  for prompt building & injection
      independent_prefetch_tasks -- [{agent_id, subagent_id, task_id}]  for prefetch section
      sa_task_ids              -- {subagent_id: set(task_ids)} for sanitizer

    Prefetch eligibility is derived from PREFETCH_TASK_RUNNERS membership (the
    dispatch table is the single source of truth), NOT from a DB flag.
    """
    available_agents: list[dict] = []
    sub_agents: dict[str, list] = {}
    independent_prefetch_tasks: list[dict] = []
    sa_task_ids: dict[str, set] = {}

    for agent_row in rows:
        available_agents.append({
            "id":          agent_row["id"],
            "label":       agent_row["label"],
            "color":       agent_row["color"],
            "description": agent_row.get("description") or "",
        })
        agent_sas: list = []

        for sa_row in agent_row.get("subagents", []):
            tasks = [
                Task(t["id"], t["label"], t.get("outputs") or [])
                for t in sa_row.get("tasks", [])
            ]
            agent_sas.append(SubAgent(sa_row["id"], sa_row["label"], tasks, sa_row.get("description") or ""))
            sa_task_ids[sa_row["id"]] = {t.id for t in tasks}
            for t in tasks:
                if t.id in PREFETCH_TASK_RUNNERS:
                    independent_prefetch_tasks.append({
                        "agent_id": agent_row["id"],
                        "subagent_id": sa_row["id"],
                        "task_id": t.id,
                    })

        sub_agents[agent_row["id"]] = agent_sas

    return available_agents, sub_agents, independent_prefetch_tasks, sa_task_ids


async def fetch_registry() -> tuple[list[dict], dict, dict, dict]:
    """Fetch agent registry from DB and return working dicts."""
    try:
        rows = await hasura.fetch_agent_registry()
        available_agents, sub_agents, independent_prefetch_tasks, sa_task_ids = _build_registry_from_rows(rows)
        logger.info(
            "[ok] [registry] DB fetch OK -- agents=%d  subagents=%d  tasks=%d",
            len(available_agents),
            sum(len(v) for v in sub_agents.values()),
            sum(len(t) for sas in sub_agents.values() for sa in sas for t in [sa.tasks]),
        )
        return available_agents, sub_agents, independent_prefetch_tasks, sa_task_ids
    except Exception as e:
        logger.warning("[!] [registry] DB fetch FAILED (%s) -- using hardcoded fallback", e)
        return AVAILABLE_AGENTS, SUB_AGENTS, INDEPENDENT_PREFETCH_TASKS, _SA_TASK_IDS

_fetch_registry = fetch_registry  # internal alias kept for existing call sites

SUB_AGENTS: dict[str, list[SubAgent]] = {
    'bed_agent': [
        SubAgent('sa_bed_availability', 'Bed Availability', [
            Task('ta_query_beds', 'Query all available beds — always include; returns counts by type (icu_count, hdu_count, general_count, ventilator_count, isolation_count)', ['candidate_count', 'icu_count', 'hdu_count', 'general_count', 'ventilator_count', 'isolation_count', 'candidates']),
            Task('ta_check_dirty_icu_beds', 'Fallback when ICU has no clean beds — ONLY when patient requires an ICU bed; condition: ta_query_beds.icu_count == 0', ['dirty_count', 'dirty_beds']),
            Task('ta_check_dirty_soon_to_release', 'Fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0', ['beds']),
            Task('ta_check_overflow_candidates', 'Fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0', ['candidates']),
            Task('ta_check_temporary_overflow_beds', 'Emergency fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0', ['candidates']),
        ], 'Identifies all available beds that are clean, unblocked, and ready for a patient'),
        SubAgent('sa_dirty_bed_recovery', 'Dirty Bed Recovery', [
            Task('ta_clean_vacated_beds', 'Dispatch standard housekeeping for recently discharged beds — always include for bed_cleaning task type', ['dispatched']),
            Task('ta_create_emergency_cleaning_task', 'Create priority cleaning job for urgently needed bed — condition: ta_check_dirty_icu_beds.dirty_count > 0', ['bed_ids', 'created']),
            Task('ta_dispatch_housekeeping_fast_track', 'Dispatch fast-track housekeeping with response target < 10 minutes — condition: ta_check_dirty_icu_beds.dirty_count > 0', ['dispatched', 'within_sla']),
            Task('ta_escalate_to_floor_supervisor', 'Escalate to floor supervisor when housekeeping cannot respond in time — condition: ta_dispatch_housekeeping_fast_track.within_sla == 0', ['escalated']),
            Task('ta_validate_sanitization', 'Validate sanitization completed after housekeeping dispatch — condition: ta_dispatch_housekeeping_fast_track.within_sla > 0', ['passed']),
            Task('ta_mark_bed_ready', 'Mark bed clean and available after sanitization passes — condition: ta_validate_sanitization.passed > 0', ['bed_id']),
            Task('ta_check_room_readiness', 'Check overall room cleanliness and readiness status', ['ready', 'issues']),
            Task('ta_validate_oxygen_readiness', 'Validate O2 pipeline is functional for a bed', ['functional']),
            Task('ta_check_monitor_readiness', 'Check that bedside monitor is connected and functional', ['functional']),
            Task('ta_notify_biomedical_team', 'Alert biomedical engineering about an equipment fault', ['notified']),
            Task('ta_sync_ready_status', 'Sync bed-ready status to the Bed Assignment Agent', ['synced']),
            Task('ta_create_equipment_task', 'Create an equipment setup or repair task', ['task_id']),
        ], 'Dispatches emergency housekeeping for dirty beds detected by availability check'),
        SubAgent('sa_bed_ranking', 'Bed Assignment', [
            Task('ta_rank_beds', 'Rank candidate beds for this patient — always include when reserving', ['ranked_beds', 'recommendation']),
            Task('ta_filter_ventilator_beds', 'ONLY when patient requires mechanical ventilation — condition: ta_query_beds.ventilator_count > 0', ['candidates']),
            Task('ta_filter_isolation_beds', 'ONLY when patient requires infection isolation — condition: ta_query_beds.isolation_count > 0', ['candidates']),
            Task('ta_apply_gender_filter', 'ONLY when gender-bay policy explicitly applies to this admission — condition: ta_query_beds.candidate_count > 0', ['candidates']),
            Task('ta_apply_isolation_room_filter', 'ONLY when patient requires negative-pressure isolation (TB, airborne precautions) — condition: ta_query_beds.isolation_count > 0', ['candidates']),
            Task('ta_trigger_alternate_ward_search', 'ONLY when primary ward candidates are exhausted after filtering — condition: ta_query_beds.candidate_count > 0', ['candidates']),
            Task('ta_recommend_transfer_allocation', 'ONLY when no bed available in-hospital — condition: ta_query_beds.candidate_count == 0', ['recommendation']),
            Task('ta_recommend_icu_to_ward_transfer', 'ONLY when ICU bed is needed but ICU is full — condition: ta_query_beds.icu_count == 0', ['recommendation']),
            Task('ta_allocate_overflow_bed', 'ONLY when no standard bed available, last resort, AND patient is NOT ventilator-dependent or monitoring-dependent — for vent/monitoring-dependent patients external transfer is the correct last resort, not overflow bed allocation', ['bed_id']),
        ], "Uses clinical AI to recommend the best available bed based on the patient's needs"),
        SubAgent('sa_bed_reservation', 'Bed Reservation', [
            Task('ta_create_approval', 'Lock bed and create approval task — always include when reserving', ['approval_id', 'bed_id']),
            Task('ta_confirm_reservation', 'Confirm reservation post-approval — always include when reserving', ['bed_id', 'status']),
            Task('ta_sync_bed_status', 'Sync bed status with HIS after reservation — always include when reserving', ['synced']),
            Task('ta_hold_bed_temporarily', 'Soft-hold a bed for a patient explicitly en route but not yet arrived — ONLY when goal mentions patient en route', ['held']),
        ], 'Reserves the selected bed and notifies the receiving ward after clinical approval'),
        SubAgent('sa_escalation', 'Escalation', [
            Task('ta_escalate_to_command_center', 'Broadcast a full escalation alert via WebSocket', ['escalated']),
            Task('ta_escalate_allocation_conflict', 'Escalate when no compliant bed can be found after full search', ['escalated']),
        ], 'Escalates when no compliant bed can be found after a full search'),
        SubAgent('sa_bed_pred_census', 'Capacity Census', [
            Task('ta_get_capacity_snapshot', 'Count beds, ICU occupancy, discharge horizon, and ER pressure', ['total_beds', 'available', 'icu_occupancy_pct', 'overflow_risk']),
        ], 'Counts beds, ICU occupancy, discharge horizon, and ER pressure'),
    ],
    'icu_agent': [
        SubAgent('sa_icu_census', 'ICU Census', [
            Task('ta_get_icu_census', 'Query ICU occupancy, current admissions, and available beds from Redis', ['icu_available', 'available_beds', 'icu_admissions', 'non_icu_admissions']),
        ], 'Reviews current ICU occupancy, active admissions, and available beds (data only; step-down flagging belongs to the Step-Down Coordinator)'),
        SubAgent('sa_icu_transfer', 'ICU Admissions', [
            Task('ta_rank_icu_requests', 'Rank incoming ICU admission requests by clinical acuity — always run when ICU requests are active', ['ranked_requests', 'ventilator_dependent_count', 'deterioration_risk_count']),
            Task('ta_prioritize_ventilator_bed', 'Prioritize ventilator ICU bed for ventilator-dependent patients — condition: ta_rank_icu_requests.ventilator_dependent_count > 0', ['ventilator_priority_count', 'ranked_requests']),
            Task('ta_reserve_icu_admission', 'Reserve ICU admission for top-ranked patient when beds available — condition: ta_get_icu_census.icu_available > 0', ['approval_id', 'patient_token']),
            Task('ta_trigger_overflow_evaluation', 'Trigger overflow evaluation when ICU full — condition: ta_get_icu_census.icu_available == 0', ['overflow_triggered', 'patients_pending']),
            Task('ta_escalate_deterioration', 'Escalate priority for patients with high deterioration risk — condition: ta_rank_icu_requests.deterioration_risk_count > 0', ['escalated']),
        ], 'Handles incoming ICU admissions: ranks pending admission requests by acuity, reserves a bed for the most critical patient, and triggers overflow evaluation when ICU is full. Transfers OUT of ICU (step-down) are handled by the Step-Down Coordinator.'),
        SubAgent('sa_icu_stepdown', 'Step-Down Coordinator', [
            Task('ta_analyze_icu_status', 'Analyse step-down and escalation eligibility with Claude', ['step_down_candidates', 'escalation_candidates', 'summary', 'critical_vital_ids', 'transfer_candidate_count']),
            Task('ta_create_icu_approval', 'Create ICU transfer approval task in Hasura — condition: ta_analyze_icu_status.transfer_candidate_count > 0', ['created']),
            Task('ta_confirm_icu_actions', 'Execute confirmed step-down transfers', ['critical_vitals_flagged', 'transfers_staged']),
        ], 'Confirms clinical criteria for STEP-DOWN -- an INTERNAL transfer of an ICU patient who is STAYING in the hospital to a lower-acuity bed (ward / HDU / progressive care) -- and arranges that transfer. Include ONLY when the task is an internal ICU-to-ward step-down. Do NOT use for a FULL HOSPITAL DISCHARGE (patient leaving the hospital -- that is discharge_agent), nor for a documentation / notes / records review (which is not a transfer at all).'),
    ],
    'er_agent': [
        SubAgent('sa_er_triage', 'Triage Monitor', [
            Task('ta_get_er_visits', 'Query the active ER visit queue from Redis — always include; the data spine every downstream ER sub-agent reads from', ['visits']),
            Task('ta_triage_patients', 'Score and triage ER patients with Claude — always include when ER visits exist; emits the criticality counts downstream tasks branch on', ['triaged', 'ctas1', 'ctas2', 'critical', 'spo2_critical_count', 'protocol_flags_count', 'specialist_needed_count', 'fasttrack_count', 'admission_candidate_count']),
            Task('ta_save_triage_scores', 'Persist triage scores to Redis — always include after triage', ['saved']),
        ], 'Pulls the active ER queue and scores/persists CTAS triage for every patient; the always-run spine that emits the criticality counts every other ER sub-agent branches on'),
        SubAgent('sa_er_acuity_response', 'Acuity Response', [
            Task('ta_detect_cardiac_arrest', 'Trigger code-blue workflow when cardiac arrest is suspected — condition: ta_triage_patients.ctas1 > 0', ['cardiac_arrest_suspected', 'code_blue_triggered']),
            Task('ta_check_spo2_critical', 'Escalate stabilization protocol when SpO2 is critically low — condition: ta_triage_patients.spo2_critical_count > 0', ['spo2_critical', 'escalated']),
            Task('ta_detect_clinical_protocol', 'Activate sepsis/stroke/trauma protocol pathway — condition: ta_triage_patients.protocol_flags_count > 0', ['protocol_count', 'protocol_activated', 'protocols']),
            Task('ta_notify_specialist', 'Notify relevant specialist team based on detected condition — condition: ta_triage_patients.specialist_needed_count > 0', ['notified', 'specialists_notified']),
        ], 'Reactive emergency handling for triaged patients — code-blue, SpO2 stabilization, sepsis/stroke/trauma protocols, specialist paging; runs ONLY when triage flags criticality'),
        SubAgent('sa_er_disposition', 'Disposition Coordinator', [
            Task('ta_route_fasttrack', 'Route low-acuity patients (CTAS 4-5) to fast-track / OPD diversion — condition: ta_triage_patients.fasttrack_count > 0', ['fasttrack_candidates']),
            Task('ta_select_critical', 'Select critical patients (CTAS 1-3) for admission and tag bed_type_needed for the Bed Agent — condition: ta_triage_patients.admission_candidate_count > 0', ['critical_patients']),
        ], 'Routes triaged patients to their next setting — fast-track/OPD for low acuity, admission selection with bed-type tagging for high acuity'),
        SubAgent('sa_er_boarding', 'Boarding Monitor', [
            Task('ta_check_er_boarders', 'Check ER boarders and escalate SLA breaches', ['boarders', 'escalated']),
        ], 'Checks ER BOARDERS -- patients ALREADY ADMITTED who are physically still in the ED waiting for an inpatient bed -- and escalates bed-wait SLA breaches. Include ONLY when the concern is admitted patients boarding in the ED (bed-block / boarding SLA). Do NOT include for the not-yet-triaged / unattended active ER queue, general ER wait times, or ER understaffing -- that active-queue crisis belongs to the Triage Monitor, not boarding. Boarding tracks a different population (admitted, awaiting a bed) than the arrival/triage queue.'),
    ],
    'staff_agent': [
        SubAgent('sa_ratio_monitor', 'Ratio Monitor', [
            Task('ta_get_ward_workload', 'Aggregate patients and incomplete/overdue task load per ward (from admissions + clinical tasks)', ['workload']),
            Task('ta_get_hourly_workload', 'Bucket ward task-load by hour of day; flag peak / understaffed hours', ['by_hour', 'peak_hours', 'understaffed_hours', 'total_tasks']),
            Task('ta_get_area_staffing', 'Assess staffing for a specific area (front desk, OPD, phlebotomy, OT, recovery/PACU, lab, inpatient nursing); flag understaffed areas', ['shift', 'areas_assessed', 'areas', 'understaffed_areas']),
            Task('ta_check_documentation_gaps', 'Detect staffing documentation gaps (missing / overdue care notes, charting, unsigned records) by ward — ONLY for administrative/audit/compliance goals; exclude during live emergency, real-time staffing, or float-pool dispatch', ['documentation_tasks_pending', 'documentation_tasks_overdue', 'by_ward', 'flagged_wards', 'has_gaps']),
            Task('ta_analyze_staff_workload', 'Analyse ward workload; flag high-pressure wards and recommend same-type staff moves (Claude)', ['recommendations', 'high_pressure_wards', 'summary']),
        ], 'Reviews nurse-to-patient ratios across all wards and flags understaffed areas'),
        SubAgent('sa_float_pool', 'Float Pool Dispatcher', [
            Task('ta_create_staff_approval', 'Create float pool deployment approval', ['created']),
            Task('ta_confirm_staff_recommendation', 'Confirm and dispatch float nurses — condition: ta_create_staff_approval.created == true', ['status', 'recommendations']),
        ], 'Covers staffing shortfalls by identifying available float nurses and deploying them to understaffed wards'),
    ],
    'ambulance_agent': [
        SubAgent('sa_ambulance_census', 'Fleet Census', [
            Task('ta_get_available_ambulances', 'Fetch ambulance fleet data', ['ambulances']),
        ], 'Fetches the full ambulance fleet from the database and caches it in Redis for fast dispatch lookup'),
        SubAgent('sa_ambulance_dispatch', 'Dispatch Coordinator', [
            Task('ta_assign_ambulance', 'Assign best available unit, surface ETA and crew, flag escalation', ['assigned_vehicle_no', 'eta_mins', 'escalate']),
            Task('ta_create_ambulance_approval', 'Create dispatch approval task — condition: ta_assign_ambulance.assigned_vehicle_no != null', ['approval_id']),
            Task('ta_confirm_ambulance_dispatch', 'Confirm ambulance dispatch — condition: ta_create_ambulance_approval.approval_id != null', ['confirmed']),
        ], 'Assigns the best available unit by type and fuel level, surfaces ETA and crew from the DB record, and flags escalation for critical emergency types'),
    ],
    'discharge_agent': [
        SubAgent('sa_discharge_ready', 'Readiness Assessor', [
            Task('ta_get_discharge_candidates', 'Fetch active admissions and discharge checklists', ['candidates', 'count']),
            Task('ta_batch_assess_discharges', 'Assess discharge readiness for each patient', ['assessed', 'ready', 'blocked']),
            Task('ta_check_notes_completeness', 'Check all clinical notes are present for discharge-ready patients — condition: ta_batch_assess_discharges.ready > 0', ['notes_incomplete', 'incomplete_admissions']),
            Task('ta_request_missing_docs', 'Request missing documentation — condition: ta_check_notes_completeness.notes_incomplete > 0', ['requested']),
            Task('ta_check_pending_results', 'Check for pending lab/imaging results before finalizing summary — condition: ta_batch_assess_discharges.ready > 0', ['results_pending', 'admissions_with_pending']),
            Task('ta_generate_summaries', 'Generate AI discharge summaries — always include when discharge-ready patients exist; marks any pending lab/imaging results in the draft rather than blocking on them', ['summaries_generated']),
        ], 'Reviews each admitted patient to determine if they are clinically ready for discharge — ONLY for active admissions and forward-looking readiness goals; exclude for retrospective, audit, or post-discharge review'),
        SubAgent('sa_discharge_retrospective', 'Retrospective Discharge Reviewer', [
            Task('ta_get_discharge_records', 'Fetch recently discharged / closed encounters — ONLY for retrospective, audit, or post-discharge review goals; exclude for active admission readiness assessment', ['records', 'count']),
            Task('ta_generate_summaries', 'Generate AI discharge summaries — always include when discharge-ready patients exist; marks any pending lab/imaging results in the draft rather than blocking on them', ['summaries_generated']),
        ], 'Reviews completed/closed discharge encounters for documentation and audit goals — ONLY for retrospective, historical, or post-discharge review; NOT for active admissions'),
        SubAgent('sa_discharge_barriers', 'Discharge Approver', [
            Task('ta_create_discharge_approval', 'Create discharge approval task in Hasura', ['approval_id']),
            Task('ta_confirm_discharge_updates', 'Execute confirmed discharge updates', ['confirmed']),
        ], 'Awaits human approval then commits discharge-ready status and frees the bed'),
    ],
    'pharmacy_agent': [
        SubAgent('sa_stock_monitor', 'Stock Monitor', [
            Task('ta_get_discharge_patients', 'Fetch discharge-ready patients for med reconciliation', ['patients']),
            Task('ta_check_medication_reconciliation', 'Check medication reconciliation gaps', ['gaps', 'stock_hours_remaining']),
            Task('ta_save_pharmacy_report', 'Persist pharmacy report to Hasura', ['saved']),
        ], 'Reviews current medication stock levels and flags drugs running low'),
        SubAgent('sa_medication_prioritization', 'Medication Prioritization Agent', [
            Task('ta_check_stat_medication_orders', 'Check for STAT medication orders', ['stat_count', 'stat_orders']),
            Task('ta_apply_critical_patient_priority', 'Apply highest priority for critical and ICU/ER patients', ['critical_patient_count', 'prioritized_count']),
            Task('ta_check_stat_availability', 'Check if STAT medications are in stock', ['stat_available_count', 'stat_unavailable_count']),
            Task('ta_escalate_stat_shortage', 'Escalate when STAT medication is unavailable', ['escalated']),
        ], 'Prioritizes STAT and critical patient medication orders; escalates unavailability'),
        SubAgent('sa_medication_fulfillment', 'Medication Fulfillment Agent', [
            Task('ta_check_prescription_received', 'Check if prescriptions have been received for pending orders', ['prescription_count', 'pending_count']),
            Task('ta_check_medication_availability', 'Verify medication is available in inventory', ['available_count', 'unavailable_count']),
            Task('ta_track_dispensing_progress', 'Track in-progress dispensing orders', ['dispensing_count', 'delayed_count']),
            Task('ta_close_fulfilled_orders', 'Mark completed medication orders as closed', ['closed_count']),
        ], 'Tracks prescription receipt, availability check, and dispensing lifecycle'),
        SubAgent('sa_drug_availability', 'Drug Availability Agent', [
            Task('ta_check_stock_levels', 'Check current medication stock vs reorder levels', ['low_stock_count', 'out_of_stock_count', 'adequate_count']),
            Task('ta_search_alternate_location', 'Search satellite pharmacy and ICU cart for alternative stock', ['alternate_found', 'alternate_location']),
            Task('ta_reserve_inventory', 'Reserve available inventory for STAT and critical orders', ['reserved_count']),
            Task('ta_escalate_critical_shortage', 'Escalate shortage impacting patient care to pharmacy lead', ['escalated', 'shortage_medications']),
        ], 'Monitors stock levels, searches alternate locations, reserves inventory'),
        SubAgent('sa_prescription_validation', 'Prescription Validation Agent', [
            Task('ta_validate_prescription_completeness', 'Check prescriptions are complete with all required fields', ['complete_count', 'incomplete_count']),
            Task('ta_validate_dosage_range', 'Validate dose is within therapeutic safe range', ['safe_count', 'unsafe_dose_count']),
            Task('ta_detect_duplicate_medications', 'Detect duplicate medication orders for same patient', ['duplicate_count', 'duplicates']),
            Task('ta_approve_or_hold_prescription', 'Release prescription for dispensing or hold for review', ['approved_count', 'held_count']),
        ], 'Validates completeness, dosage safety, and duplicate detection before dispensing'),
        SubAgent('sa_clinical_interaction', 'Clinical Interaction Agent', [
            Task('ta_check_polypharmacy', 'Check patients prescribed multiple concurrent medications', ['polypharmacy_count', 'patient_count']),
            Task('ta_run_interaction_check', 'Run drug-drug interaction check against known rules', ['major_interaction_count', 'total_interactions']),
            Task('ta_check_allergy_conflict', 'Check for allergy conflicts in pending orders', ['allergy_conflict_count']),
            Task('ta_approve_safe_dispense', 'Approve or hold medication based on interaction findings', ['approved_count', 'held_count']),
        ], 'Runs drug-drug interaction and allergy conflict checks'),
        SubAgent('sa_dispensing_validation', 'Dispensing Validation Agent', [
            Task('ta_verify_patient_identity', 'Verify patient identity before medication dispensing', ['verified_count', 'unverified_count']),
            Task('ta_match_medication_prescription', 'Match dispensed medication to prescription', ['match_failed_count', 'matched_count']),
            Task('ta_validate_dispensing_dosage', 'Validate correct dosage before medication release', ['dose_mismatch_count', 'dose_correct_count']),
            Task('ta_release_or_hold_dispensing', 'Release verified medication or hold discrepancy for review', ['released_count', 'held_count']),
        ], 'Right patient / right drug / right dose validation before medication release'),
        SubAgent('sa_medication_substitution', 'Medication Substitution Agent', [
            Task('ta_check_unavailable_medications', 'Identify prescribed medications that are unavailable', ['unavailable_count', 'unavailable_drugs']),
            Task('ta_search_formulary_alternatives', 'Search formulary for therapeutic substitutes', ['substitute_available', 'alternatives_found']),
            Task('ta_request_physician_approval', 'Send substitution approval request to prescribing physician', ['approval_sent', 'approved_count']),
            Task('ta_update_substitution_order', 'Update order with approved substitute medication', ['orders_updated']),
        ], 'Recommends formulary alternatives and manages physician approval workflow'),
        SubAgent('sa_pharmacy_queue', 'Pharmacy Queue Optimization Agent', [
            Task('ta_check_queue_length', 'Check current dispensing queue length and STAT backlog', ['queue_length', 'stat_waiting_count', 'queue_above_threshold']),
            Task('ta_analyze_queue_bottleneck', 'Identify bottleneck causing queue buildup', ['bottleneck_stage', 'bottleneck_count']),
            Task('ta_prioritize_stat_medications', 'Reprioritize queue for pending STAT medication orders', ['reprioritized_count']),
            Task('ta_escalate_tat_breach', 'Escalate to pharmacy supervisor when TAT SLA is breached', ['escalated', 'breach_count']),
        ], 'Monitors dispensing queue, identifies bottlenecks, prioritizes STAT workload'),
        SubAgent('sa_controlled_drug_compliance', 'Controlled Drug Compliance Agent', [
            Task('ta_identify_controlled_orders', 'Identify controlled substance orders requiring audit', ['controlled_count', 'controlled_orders']),
            Task('ta_verify_controlled_authorization', 'Verify authorization and witness documentation', ['authorized_count', 'missing_auth_count']),
            Task('ta_check_inventory_variance', 'Check for controlled drug inventory discrepancies', ['variance_detected', 'variance_count']),
            Task('ta_escalate_compliance_issue', 'Escalate to compliance officer for investigation', ['escalated']),
        ], 'Audits controlled substance dispensing for authorization and inventory accuracy'),
    ],
    'lab_agent': [
        SubAgent('sa_sample_prioritization', 'Sample Prioritization Agent', [
            Task('ta_check_stat_status', 'Check if any STAT samples are pending', ['stat_count', 'stat_samples']),
            Task('ta_apply_icu_er_priority', 'Apply highest priority for ICU/ER samples', ['prioritized_count']),
            Task('ta_check_analyzer_available', 'Check if target analyzer is available', ['available_count']),
            Task('ta_escalate_tat_risk', 'Escalate to supervisor if TAT target is at risk', ['escalated']),
        ], 'Prioritizes STAT/ICU/ER samples and escalates TAT-risk cases'),
        SubAgent('sa_sample_tracking', 'Sample Tracking Agent', [
            Task('ta_check_sample_collection', 'Check which samples are collected vs pending', ['collected_count', 'pending_count']),
            Task('ta_check_sample_transport', 'Check transport status for collected samples', ['in_transit', 'delayed_count']),
            Task('ta_verify_sample_receipt', 'Verify sample received at lab', ['received_count', 'missing_count']),
            Task('ta_trigger_sample_search', 'Trigger search for misplaced samples', ['search_triggered']),
        ], 'Tracks collection, transport, and lab receipt status; triggers search for missing samples'),
        SubAgent('sa_tat_optimization', 'TAT Optimization Agent', [
            Task('ta_check_tat_threshold', 'Check if current TAT exceeds SLA threshold', ['overdue_count', 'tat_exceeded']),
            Task('ta_analyze_tat_bottleneck', 'Identify the processing bottleneck stage', ['bottleneck_stage', 'bottleneck_count']),
            Task('ta_prioritize_stat_queue', 'Reprioritize queue for pending STAT samples', ['reprioritized_count']),
            Task('ta_escalate_tat_supervisor', 'Escalate to Lab Supervisor when TAT is not restored', ['escalated']),
        ], 'Monitors turnaround time, identifies bottlenecks, escalates SLA breaches'),
        SubAgent('sa_analyzer_utilization', 'Analyzer Utilization Agent', [
            Task('ta_check_analyzer_utilization', 'Check if any analyzer load exceeds 90%', ['overloaded_count', 'max_utilization']),
            Task('ta_identify_alternate_analyzer', 'Identify available backup analyzer', ['alternate_available', 'alternate_id']),
            Task('ta_rebalance_analyzer_workload', 'Rebalance workload to backup analyzer', ['rebalanced']),
            Task('ta_trigger_maintenance_alert', 'Alert maintenance team for predicted downtime', ['alerted']),
        ], 'Monitors analyzer load, triggers rebalancing and maintenance alerts'),
        SubAgent('sa_analyzer_routing', 'Analyzer Routing Agent', [
            Task('ta_check_analyzer_overload', 'Check if primary analyzer is overloaded', ['overloaded', 'load_pct']),
            Task('ta_validate_alternate_analyzer', 'Validate backup analyzer is certified for the test', ['validated', 'alternate_id']),
            Task('ta_execute_sample_routing', 'Route samples to alternate analyzer', ['routed_count']),
            Task('ta_restore_routing_capacity', 'Close routing workflow when capacity is normalized', ['restored']),
        ], 'Routes samples to alternate analyzers when primary is overloaded'),
        SubAgent('sa_quality_control', 'Quality Control Agent', [
            Task('ta_check_qc_status', 'Check QC pass/fail for active analyzers this shift', ['failed_count', 'qc_failed']),
            Task('ta_trigger_recalibration', 'Stop result release and trigger recalibration', ['recalibration_triggered']),
            Task('ta_repeat_qc_check', 'Rerun QC after calibration', ['passed', 'repeat_passed']),
            Task('ta_compliance_alert', 'Generate compliance alert if accreditation is impacted', ['alerted']),
        ], 'Validates QC pass/fail per shift, triggers recalibration, raises compliance alerts'),
        SubAgent('sa_test_validation', 'Test Validation Agent', [
            Task('ta_validate_result_rules', 'Check result against auto-validation rules', ['auto_released', 'flagged_count']),
            Task('ta_check_delta_flag', 'Run delta check against prior result', ['delta_failed_count']),
            Task('ta_check_critical_value_flag', 'Check for critical value requiring escalation', ['critical_count']),
            Task('ta_release_validated_report', 'Release auto-validated reports', ['released_count']),
        ], 'Auto-validates results against rules, delta checks, critical value flags'),
        SubAgent('sa_critical_result_escalation', 'Critical Result Escalation Agent', [
            Task('ta_detect_critical_results', 'Detect critical lab values requiring immediate action', ['critical_count', 'critical_results']),
            Task('ta_notify_physician_critical', 'Alert physician for critical result', ['notified_count']),
            Task('ta_escalate_icu_er_critical', 'Trigger urgent escalation for ICU/ER patients', ['escalated_count']),
            Task('ta_log_critical_action', 'Log physician acknowledgment and close workflow', ['logged']),
        ], 'Detects critical lab values and escalates to physician / ICU-ER team'),
        SubAgent('sa_test_recommendation', 'Test Recommendation Agent', [
            Task('ta_detect_abnormal_result', 'Detect abnormal results triggering reflex rules', ['abnormal_count', 'abnormal_results']),
            Task('ta_evaluate_reflex_rules', 'Apply reflex/add-on rules to abnormal results', ['recommended_count']),
            Task('ta_recommend_additional_test', 'Send recommendation to physician', ['sent_count']),
            Task('ta_create_reflex_order', 'Auto-create order per protocol if no approval needed', ['orders_created']),
        ], 'Detects abnormal results, applies reflex rules, recommends add-on tests'),
    ],
    'ot_agent': [
        SubAgent('sa_ot_census', 'OT Census', [
            Task('ta_get_ot_census', "Fetch OT schedule, rooms, room status, today's equipment, and available post-op (ICU/HDU) beds from cache — always include", ['schedule', 'rooms', 'room_status', 'upcoming_surgeries', 'upcoming_count', 'post_op_beds_available', 'icu_available', 'hdu_available']),
        ], "Reviews today's surgical list, theatre status, equipment, and available post-operative (ICU/HDU) beds"),
        SubAgent('sa_ot_turnaround', 'OT Turnaround Agent', [
            Task('ta_ot_check_cleaning', 'OT cleaning coordination — check which theatres need cleaning before next case — always include', ['cleaning_count', 'rooms_to_clean']),
            Task('ta_ot_check_instruments', 'Instrument readiness validation — validate equipment availability for upcoming surgeries — condition: ta_get_ot_census.upcoming_count > 0', ['gap_count', 'ready_count', 'gaps']),
            Task('ta_ot_track_turnaround', 'OT turnaround tracking — track theatre utilisation and active surgeries — always include', ['rooms_active', 'active_count', 'utilisation_pct']),
            Task('ta_ot_predict_delays', 'Delay prediction — predict on-time start risks per theatre — always include', ['delay_risks', 'high_risk_count']),
            Task('ta_ot_coordinate_staff', 'Staff coordination — assign staff actions to address delay risks and gaps — condition: ta_ot_predict_delays.high_risk_count > 0', ['staff_actions']),
        ], 'Reduces OT idle time — coordinates theatre cleaning, validates instrument readiness, predicts delays, and dispatches preparation tasks'),
        SubAgent('sa_ot_scheduling', 'OT Scheduling Agent', [
            Task('ta_ot_detect_conflicts', 'Conflict detection — detect room double-bookings and surgeon overlaps — always include', ['conflict_count', 'has_conflicts', 'room_conflicts', 'surgeon_conflicts']),
            Task('ta_ot_check_resources', 'Resource-aware scheduling — assess room availability and case load per theatre — always include', ['available_rooms', 'utilisation_pct', 'under_resourced']),
            Task('ta_ot_optimise_slots', 'Surgery slot optimisation — recommend slot swaps to resolve conflicts — condition: ta_ot_detect_conflicts.has_conflicts == true', ['slot_optimizations']),
            Task('ta_ot_balance_load', 'OT load balancing — balance case load across available theatres — always include', ['load_balance', 'summary']),
            Task('ta_ot_find_theatre_slots', 'Find open theatre slots — derive bookable OT time-windows (by room type + duration) for (re)scheduling a surgery — include when a surgery must be scheduled or rescheduled into a slot', ['open_slots', 'open_slot_count']),
            Task('ta_ot_reschedule_surgery', 'Reschedule a cancelled surgery to the earliest open theatre slot; stages an executable move for commit — ONLY when a surgery is cancelled / must be moved to a new slot', ['rescheduled', 'proposals', 'status']),
        ], 'Owns the surgical plan AND surgical (re)scheduling: detects room/surgeon conflicts, checks resources, balances theatre load, and -- when a surgery is cancelled or must be moved -- finds an open theatre slot and stages an executable reschedule. This is the owner of moving/rescheduling a theatre case (never the OPD appointment agent).'),
        SubAgent('sa_ot_emergency', 'OT Emergency Response', [
            Task('ta_ot_find_emergencies', 'Emergency detection -- identify non-elective and emergency cases in the schedule -- always include', ['emergency_count', 'emergency_cases']),
            Task('ta_ot_handle_emergencies', 'Emergency OT handling -- plan immediate actions for emergency cases -- condition: ta_ot_find_emergencies.emergency_count > 0', ['emergency_actions']),
        ], 'Acuity-reactive -- detects non-elective/emergency surgical cases and plans immediate theatre assignment for them. The handling step runs only when emergency cases are present.'),
        SubAgent('sa_ot_analysis', 'OT Analysis', [
            Task('ta_ot_score_efficiency', 'OT efficiency optimisation — calculate overall OT efficiency score from delays, instrument gaps, and conflicts — always include', ['efficiency_score']),
            Task('ta_analyze_ot_capacity', 'Per-case disposition — recommend proceed/delay/escalate for each case from conflicts, emergencies, and capacity — always include', ['case_recommendations', 'recommendation_count', 'escalate_count', 'delay_count', 'proceed_count', 'summary']),
            Task('ta_ot_defer_electives', 'Reprioritisation (executable) — move the electives flagged delay to a later open theatre slot to free capacity for higher-acuity cases; stages the moves for commit — include when electives must yield to a non-elective/emergency or a conflict', ['deferred', 'proposals', 'status']),
        ], 'Terminal synthesis — scores OT efficiency, produces per-case proceed/delay/escalate recommendations, AND (executable) defers the delay-flagged electives to later slots to free capacity'),
    ],
    'revenue_agent': [
        SubAgent('sa_rev_optimization', 'Revenue Optimization', [
            Task('ta_identify_revenue_leakage', 'Identify revenue leakage across departments and workflows — always include', ['leakage_detected', 'leakage_amount', 'unbilled_count']),
            Task('ta_optimize_package_utilization', 'Optimize treatment and insurance package utilization — ONLY when goal involves package billing or package optimization', ['packages_reviewed', 'savings_identified', 'recommendations']),
            Task('ta_analyze_resource_utilization', 'Analyze utilization efficiency across hospital resources — always include', ['utilization_score', 'idle_equipment_count', 'bottlenecks']),
            Task('ta_analyze_dept_profitability', 'Analyze profitability across departments and specialties — ONLY when goal involves department performance or profitability analysis', ['dept_count', 'below_target_count', 'recommendations']),
        ], 'Identifies revenue leakage and optimizes utilization across hospital resources'),
        SubAgent('sa_rev_denial_prevention', 'Denial Prevention', [
            Task('ta_predict_denial_risk_rev', 'Predict insurance claim denial risk before submission — always include', ['high_risk_count', 'medium_risk_count', 'denial_probability']),
            Task('ta_presubmission_validation_rev', 'Validate claims before payer submission — ONLY when goal is NOT a snapshot, report, or dashboard; condition: ta_predict_denial_risk_rev.high_risk_count > 0', ['validation_passed', 'issues_found', 'missing_fields_count']),
            Task('ta_payer_rule_compliance_rev', 'Validate claims against payer-specific policies — always include', ['compliance_issues', 'non_covered_count', 'auth_missing_count']),
            Task('ta_detect_missing_docs_rev', 'Detect incomplete documentation before claim submission — always include', ['missing_docs_count', 'missing_summaries', 'missing_signatures']),
            Task('ta_escalation_recommendations_rev', 'Recommend escalation actions for high-risk revenue cases — condition: ta_predict_denial_risk_rev.high_risk_count > 0', ['escalated', 'escalation_count']),
        ], "Pre-submission claims safeguards: predicts insurance denial risk and validates claims against payer rules and require documentation before a claim is submitted to the payer. Belongs to admission / pre-authorization / claim-submission flows where claims are being prepared. Not a billing lookup or completeness check — to retrieve or verify an existing patient's billing record (e.g. at discharge), use Patient Billing Lookup."),
    ],
    'billing_agent': [
        SubAgent('sa_claim_validation', 'Claim Validation', [
            Task('ta_detect_claim_discrepancies', 'Detect duplicate claims, missing invoice linkages, and amount mismatches', ['discrepancy_count', 'missing_invoice', 'duplicate_claims']),
            Task('ta_validate_insurance_eligibility', 'Check all pending claims for missing TPA linkage and unverified insurance', ['eligibility_issues', 'no_tpa_pending_count', 'unverified_amount']),
            Task('ta_check_billing_compliance', 'Flag claims and invoices missing required fields or violating coding rules', ['total_compliance_issues', 'claim_compliance_issues', 'invoice_compliance_issues']),
        ], 'Detects duplicate claims, missing invoice linkages, and amount mismatches'),
        SubAgent('sa_billing_optimization', 'Billing Optimization', [
            Task('ta_track_pending_payments', 'Bucket overdue invoices by SLA and flag high-value accounts for follow-up', ['overdue_count', 'overdue_amount', 'high_value_count']),
            Task('ta_detect_revenue_leakage', 'Find claims without invoices and low-value IPD billing gaps', ['unlinked_claims_count', 'estimated_leakage', 'leakage_detected']),
            Task('ta_generate_billing_recommendations', 'Synthesises billing state into prioritised optimisation recommendations', ['recommendation_count', 'recommendations']),
            Task('ta_prioritize_payments', 'Rank outstanding invoices by value × aging for targeted collection', ['prioritized_count', 'total_recoverable', 'top_priority']),
            Task('ta_trigger_payment_reminder', 'Send payment reminders for overdue invoices — condition: ta_track_pending_payments.overdue_count > 0', ['reminders_sent']),
            Task('ta_notify_followup_team', 'Notify follow-up team about claims awaiting payer response — condition: ta_validate_insurance_eligibility.no_tpa_pending_count > 0', ['notified']),
        ], 'Tracks overdue invoices, detects revenue leakage, and generates billing recommendations'),
        SubAgent('sa_rev_patient_billing', 'Patient Billing Lookup', [
            Task('ta_get_patient_invoices', 'Fetch all invoices and claims for a specific patient', ['invoices', 'claims']),
        ], 'Fetches all invoices and claims for a specific patient'),
        SubAgent('sa_rev_initiate_billing', 'Initiate Billing', [
            Task('ta_create_billing_request', 'Create a bill-generation request for the resolved patient(s) -- always include when the goal is to initiate/generate/raise a bill or invoice for a patient', ['billing_requests', 'patient_count', 'status']),
        ], 'Creates a bill-generation request for the resolved patient(s); the DB side turns it into an actual bill when the session is committed'),
    ],
    'patient_verification_agent': [
        SubAgent('sa_patient_identification', 'Patient Identification', [
            Task('ta_identify_patients', 'Identify incoming patient(s) by mobile and resolve token + vitals -- always include', ['verified_count', 'unknown_count', 'patients']),
            Task('ta_flag_unknown_patients', 'Flag incoming patient(s) with no record -- condition: ta_identify_patients.unknown_count > 0', ['flagged']),
        ], 'Resolves incoming patient(s) by mobile -> token + vitals; flags unknown/provisional patients'),
        SubAgent('sa_patient_registration', 'Patient Registration', [
            Task('ta_register_patient', 'Request registration of the unknown incoming patient(s) and pause until created', ['requested', 'registered', 'still_unknown', 'patients']),
        ], 'Registers an incoming patient that has no DB record yet -- sends a registration request to Fabric (forwarded to the DB side, created manually by hospital staff) and pauses the flow until the new record is reported back, then rebinds the patient. Runs only when identification finds an unknown patient.'),
    ],
}

# Pre-built lookup: subagent_id -> set of valid task_ids from the catalog
_SA_TASK_IDS: dict[str, set[str]] = {
    sa.id: {t.id for t in sa.tasks}
    for agent_sas in SUB_AGENTS.values()
    for sa in agent_sas
}

# Prefetch eligibility is task-level: a task is prefetchable iff it has a runner
# in graph.prefetch.PREFETCH_TASK_RUNNERS. This derived list of
# {agent_id, subagent_id, task_id} triples is the hardcoded fallback used when
# the DB registry is unavailable, mirroring _build_registry_from_rows.
INDEPENDENT_PREFETCH_TASKS: list[dict] = [
    {"agent_id": agent_id, "subagent_id": sa.id, "task_id": t.id}
    for agent_id, agent_sas in SUB_AGENTS.items()
    for sa in agent_sas
    for t in sa.tasks
    if t.id in PREFETCH_TASK_RUNNERS
]


# =============================================================================
# Stage-1 agent-graph prompt (used ONLY by generate_agents_and_edges).
# Loaded from workflows/system_prompts/agent_graph.txt -- edit there, not here.
# =============================================================================
AGENT_GRAPH_SYSTEM_PROMPT = _load_prompt("agent_graph.txt")


def _build_system_prompt(
    available_agents: list[dict],
    independent_prefetch_tasks: list[dict],
    base_prompt: str,
) -> str:
    """Build the system prompt dynamically from registry data fetched from DB."""
    # The static body is everything up to the ━━━ PREFETCH ━━━ section.
    # base_prompt selects which static body to use (e.g. AGENT_GRAPH_SYSTEM_PROMPT).
    src = base_prompt
    static_end  = "━━━ PREFETCH ━━━"
    split_point = src.find(static_end)
    static_body = src[:split_point]

    # The AGENT PURPOSES section is NOT hardcoded -- it is rebuilt from the
    # agent descriptions in the hospilot_app registry. Replace the placeholder
    # block (header up to the next ━━━ section) with one line per available agent.
    purposes_header = "━━━ AGENT PURPOSES"
    p_start = static_body.find(purposes_header)
    if p_start != -1:
        # Body of the section starts after the header line; ends at the next ━━━.
        header_line_end = static_body.find("\n", p_start)
        next_section    = static_body.find("\n━━━", header_line_end)
        header_text     = static_body[p_start:header_line_end]
        id_width = max((len(a["id"]) for a in available_agents), default=0)
        purpose_lines = "\n".join(
            f"{a['id']:<{id_width}}  {a.get('description', '').strip()}"
            for a in available_agents
        )
        static_body = (
            static_body[:p_start]
            + header_text + "\n"
            + purpose_lines + "\n"
            + static_body[next_section + 1:]
        )

    # Nest the flat triples into {agent_id: {subagent_id: [task_id]}} for readability.
    nested: dict = {}
    for item in independent_prefetch_tasks:
        nested.setdefault(item["agent_id"], {}).setdefault(item["subagent_id"], []).append(item["task_id"])

    return (
        static_body
        + "━━━ PREFETCH ━━━\n"
        "Prefetch-eligible tasks are read-only (session_id only, no writes) -- they query\n"
        "Redis/Hasura directly and can fire before their parent agent workflow starts.\n\n"
        "For every agent in the pipeline, add one prefetch entry per eligible task it will run.\n"
        "Only include tasks whose parent agent appears in the pipeline and that you selected.\n\n"
        "Prefetch-eligible tasks per agent/subagent:\n"
        + json.dumps(nested, indent=2)
        + "\n\n━━━ OUTPUT ━━━\n"
        "Return ONLY valid JSON, no explanation, no markdown fences.\n\n"
        "Available agents:\n"
        + json.dumps(available_agents, indent=2)
    )


# -- Layer 4: sub-agent task sanitization -------------------------------------
# Strip any task_ids the LLM placed in the wrong sub-agent, and remove task_edges
# that reference those now-absent task_ids. This enforces catalog boundaries server-side.

def _agent_base_id(agent_id: str) -> str:
    return agent_id.split(":")[0]


def _agent_hop_groups(agents: list[dict], edges: list[dict], target_id: str) -> dict:
    """Group the OTHER agents by directed hop distance from target_id.

    BFS runs on EXACT agent ids (edges reference exact ids, including multi-instance
    suffixes like ``bed_agent:after_icu``). Returns
    ``{"upstream": {1:[e],2:[e],3:[e],"beyond":[e]}, "downstream": {...}, "unconnected": [e]}``
    where each ``e`` is ``{"id","base","role"}``. An agent reached only by following
    edges backwards is upstream (runs before); forwards is downstream (runs after).
    """
    ids = {a["id"] for a in agents}
    role_of = {a["id"]: a.get("role", "") for a in agents}
    forward: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in ids and t in ids:
            forward.setdefault(s, []).append(t)
            reverse.setdefault(t, []).append(s)

    def _bfs(adj: dict[str, list[str]]) -> dict[str, int]:
        dist: dict[str, int] = {}
        seen = {target_id}
        frontier = [target_id]
        depth = 0
        while frontier:
            depth += 1
            nxt: list[str] = []
            for node in frontier:
                for nb in adj.get(node, []):
                    if nb not in seen:
                        seen.add(nb)
                        dist[nb] = depth
                        nxt.append(nb)
            frontier = nxt
        return dist

    down, up = _bfs(forward), _bfs(reverse)

    def _entry(aid: str) -> dict:
        return {"id": aid, "base": _agent_base_id(aid), "role": role_of.get(aid, "")}

    def _bucket(dist: dict[str, int]) -> dict:
        out: dict = {1: [], 2: [], 3: [], "beyond": []}
        for aid, d in dist.items():
            out[d if d <= 3 else "beyond"].append(_entry(aid))
        return out

    connected = set(down) | set(up) | {target_id}
    unconnected = [_entry(a["id"]) for a in agents if a["id"] not in connected]
    return {"upstream": _bucket(up), "downstream": _bucket(down), "unconnected": unconnected}


def _format_hop_context(
    groups: dict,
    edges: list[dict] | None = None,
    include_subagents: bool = False,
    sub_agents_by_id: dict | None = None,
) -> str:
    """Render hop groups into prompt text. When ``include_subagents`` is set, append
    each listed agent's selected sub-agents (only meaningful once the pipeline is
    fully planned, e.g. at reorchestration)."""
    sub_agents_by_id = sub_agents_by_id or {}
    blocks: list[str] = []

    if edges:
        lines = ["Agent graph (edges):"]
        for e in edges:
            cond = e.get("condition")
            lines.append(
                f"  {e.get('source')} -> {e.get('target')}"
                + (f"  [condition: {cond}]" if cond else "")
            )
        blocks.append("\n".join(lines))

    def _entry_lines(entry: dict, hop) -> list[str]:
        hop_lbl = f"{hop} hop" if isinstance(hop, int) else hop
        rows = [f"  [{hop_lbl}] {entry['id']} -- {entry['role'] or '(no role)'}"]
        if include_subagents:
            sas = sub_agents_by_id.get(entry["id"], [])
            summary = ", ".join(
                f"{sa['id']} ({sa.get('role', sa.get('label', ''))})" for sa in sas
            )
            if summary:
                rows.append(f"      sub-agents: {summary}")
        return rows

    def _section(title: str, bucket: dict) -> str:
        rows: list[str] = []
        for hop in (1, 2, 3, "beyond"):
            for entry in bucket.get(hop, []):
                rows += _entry_lines(entry, hop)
        return "\n".join([title, *rows]) if rows else ""

    for section in (
        _section("UPSTREAM (runs before this agent):", groups.get("upstream", {})),
        _section("DOWNSTREAM (runs after this agent):", groups.get("downstream", {})),
    ):
        if section:
            blocks.append(section)

    unconn = groups.get("unconnected", [])
    if unconn:
        rows = [f"  {e['id']} -- {e['role'] or '(no role)'}" for e in unconn]
        blocks.append("\n".join(["OTHER agents (not directly connected -- context only):", *rows]))

    return "\n\n".join(blocks) if blocks else "  (no other agents in this pipeline)"


def build_graph_context(pipeline: dict, target_agent_id: str, include_subagents: bool = False) -> str:
    """Build the graph-aware pipeline context for a sub-agent-selection prompt:
    the edge list plus other agents grouped upstream/downstream by hop distance.
    Shared by stage-2 planning (include_subagents=False; neighbours' sub-agents are
    not selected yet) and reorchestration (include_subagents=True; full pipeline)."""
    agents = pipeline.get("agents", [])
    edges = pipeline.get("edges", [])
    groups = _agent_hop_groups(agents, edges, target_agent_id)
    sub_agents_by_id = (
        {a["id"]: a.get("sub_agents", []) for a in agents} if include_subagents else None
    )
    return _format_hop_context(
        groups, edges=edges, include_subagents=include_subagents,
        sub_agents_by_id=sub_agents_by_id,
    )


def _hydrate_sub_agents(agents: list[dict], sub_agents: dict) -> list[dict]:
    """Fill sub-agents/tasks from the DB catalog where the LLM left them empty.

    - Agent with no sub-agents, or whose every sub-agent has empty tasks -> replace
      with the full catalog (all sub-agents + tasks) for that agent.
    - Otherwise only fill in the tasks of individual empty sub-agents.
    Never overrides a sub-agent the LLM intentionally populated.
    """
    for agent in agents:
        catalog = sub_agents.get(_agent_base_id(agent["id"]))
        if not catalog:
            continue
        cat_by_id = {sa.id: sa for sa in catalog}
        existing = agent.get("sub_agents") or []
        populated = [sa for sa in existing if sa.get("tasks")]
        if not populated:
            agent["sub_agents"] = [sa.schema() for sa in catalog]
        else:
            for sa in existing:
                if not sa.get("tasks"):
                    src = cat_by_id.get(sa.get("id"))
                    if src:
                        sa["tasks"] = [t.schema() for t in src.tasks]
    return agents


def _sanitize_sub_agent_tasks(agents: list[dict], sa_task_ids: dict | None = None) -> list[dict]:
    task_id_map = sa_task_ids if sa_task_ids is not None else _SA_TASK_IDS

    def task_id(t: str | dict) -> str:
        return t if isinstance(t, str) else t.get("id", "")

    for agent in agents:
        # Pass 1: filter each sub-agent's tasks to catalog boundaries
        for sa in agent.get("sub_agents", []):
            valid = task_id_map.get(sa.get("id", ""))
            if not valid:
                continue
            sa["tasks"] = [t for t in sa.get("tasks", []) if task_id(t) in valid]

        # All selected task IDs across every sub-agent of this agent --
        # task_edges may cross sub-agent boundaries within the same agent
        agent_task_ids: set[str] = {
            task_id(t)
            for sa in agent.get("sub_agents", [])
            for t in sa.get("tasks", [])
        }

        # Pass 2: source must be in this sub-agent; target anywhere in this agent
        for sa in agent.get("sub_agents", []):
            present = {task_id(t) for t in sa.get("tasks", [])}
            sa["task_edges"] = [
                e for e in sa.get("task_edges", [])
                if e.get("source") in present and e.get("target") in agent_task_ids
            ]
    return agents


# -- Layer 3: missing-agent injection -----------------------------------------
# If the LLM generated icu_agent + bed_agent (reservation) but omitted discharge_agent,
# inject it -- the conditional ICU-full fallback path is always required for bed reservation.

def _inject_missing_agents(
    agents: list[dict],
    edges: list[dict],
    available_agents: list[dict] | None = None,
    sub_agents: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    _available = available_agents if available_agents is not None else AVAILABLE_AGENTS
    _sub_agents = sub_agents if sub_agents is not None else SUB_AGENTS
    agent_ids = {a["id"] for a in agents}
    base_ids  = {_agent_base_id(aid) for aid in agent_ids}

    has_icu       = "icu_agent"       in base_ids
    has_bed       = "bed_agent"       in base_ids
    has_discharge = "discharge_agent" in base_ids

    # Count bed_agent instances -- skip injection when LLM already set up multi-instance pattern
    bed_instance_count = sum(1 for a in agents if _agent_base_id(a["id"]) == "bed_agent")

    # Inject discharge_agent when icu+bed are present without it (single bed_agent only).
    # Skip when staff_agent is present -- that signals a staffing/step-down plan where
    # icu->bed is an internal transfer, not the ICU admission capacity-fallback pattern.
    has_staff = "staff_agent" in base_ids
    if has_icu and has_bed and not has_discharge and not has_staff and bed_instance_count < 2:
        icu_meta = next((a for a in agents if a["id"] == "icu_agent"), {})
        bed_meta = next((a for a in agents if _agent_base_id(a["id"]) == "bed_agent"), {})
        if (icu_meta.get("task_type") != "capacity_check"
                and bed_meta.get("task_type") not in ("availability_check", "bed_cleaning")):
            logger.info("  ↳ injecting missing discharge_agent (icu+bed co-occurrence, no discharge present)")
            discharge_def = next((a for a in _available if a["id"] == "discharge_agent"),
                                 {"id": "discharge_agent", "label": "Discharge Planning", "color": "#10b981"})
            new_agent = {
                **discharge_def,
                "role": "Identify discharge-ready patients to free capacity before bed reservation",
                "sub_agents": [sa.schema() for sa in _sub_agents.get("discharge_agent", [])],
            }
            new_edges = [e for e in edges
                         if not (e.get("source") == "icu_agent" and e.get("target") == "bed_agent")]
            new_edges += [
                {"source": "icu_agent",       "target": "discharge_agent"},
                {"source": "icu_agent",       "target": "bed_agent"},
                {"source": "discharge_agent", "target": "bed_agent"},
            ]
            agents     = list(agents) + [new_agent]
            edges      = new_edges
            agent_ids  = {a["id"] for a in agents}
            base_ids   = {_agent_base_id(aid) for aid in agent_ids}
            has_discharge = True
            has_bed    = "bed_agent" in base_ids

    # Ensure icu->bed NO-path edge exists when all three agents are present (single bed instance only)
    if has_icu and has_bed and has_discharge and bed_instance_count < 2:
        icu_meta = next((a for a in agents if a["id"] == "icu_agent"), {})
        bed_meta = next((a for a in agents if _agent_base_id(a["id"]) == "bed_agent"), {})
        if (icu_meta.get("task_type") != "capacity_check"
                and bed_meta.get("task_type") not in ("availability_check", "bed_cleaning")):
            has_icu_bed = any(
                _agent_base_id(e.get("source", "")) == "icu_agent"
                and _agent_base_id(e.get("target", "")) == "bed_agent"
                for e in edges
            )
            if not has_icu_bed:
                logger.info("  ↳ injecting missing icu->bed NO-path edge (all three agents present)")
                edges = list(edges) + [{"source": "icu_agent", "target": "bed_agent"}]

    # Removed: auto-injecting revenue_agent on discharge creates noise in pure clinical flows.
    # Revenue should only appear when the goal explicitly mentions billing or financial review.
    # if has_discharge and "revenue_agent" not in base_ids:
    #     logger.info("  ↳ injecting missing revenue_agent (discharge_agent present)")
    #     revenue_def = next((a for a in _available if a["id"] == "revenue_agent"),
    #                        {"id": "revenue_agent", "label": "Revenue", "color": "#f97316"})
    #     new_revenue = {
    #         **revenue_def,
    #         "role": "Billing follow-up after discharge -- invoicing, outstanding claims, revenue gaps",
    #         "sub_agents": [sa.schema() for sa in _sub_agents.get("revenue_agent", []) if sa.id == "sa_rev_optimization"],
    #     }
    #     agents = list(agents) + [new_revenue]
    #     edges = list(edges) + [{"source": "discharge_agent", "target": "revenue_agent"}]

    return agents, edges


# -- Layer 3b: patient-verification prerequisite injection --------------------
# Single-path identity: the consumer bodies read patient.get_cached() and no longer
# self-prompt. patient_verification_agent must LEAD (no incoming edges) with an
# unconditional edge to every consumer so identity is resolved first. This runs at
# stage 3b -- AFTER task selection -- so the trigger is the concrete task actually
# chosen, not a stage-1 task_type guess the LLM might omit.

_PV_AGENT_ID = "patient_verification_agent"

# Tasks that operate on ONE specific INCOMING patient and therefore require identity
# (mobile -> token + vitals) resolved upstream. When any is SELECTED *and* the goal is
# about a specific incoming individual (see _goal_is_incoming_individual), its owning
# agent leads with patient_verification_agent. Discharge/billing are EXCLUDED -- they run
# the SAME tasks whether one patient was named or the whole ward is swept.
#
# ta_get_er_visits was REMOVED from this set: it is the always-on ER triage spine that
# reads the EXISTING Redis queue (already-registered patients), so it dragged PV into
# EVERY ER plan -- including population-level "can the ED cope" goals -- where there is no
# incoming identity to resolve. ICU admission ranking stays: it ranks specific incoming
# admission requests.
_PATIENT_REQUIRED_TASKS = frozenset({
    "ta_rank_icu_requests",  # ICU admissions ranking (never the read-only capacity check)
})

# Bed reservation is patient-specific ONLY as a graph ENTRY POINT -- a standalone
# "reserve a bed for <incoming patient>" goal. When the reservation sits DOWNSTREAM of
# another agent it is internal flow, NOT a mobile-lookup patient: an ICU step-down or
# post-discharge ward move (internal transfer -- no mobile identity exists), or an ER/
# ICU-ranking admit (identity already resolved upstream and inherited transitively).
# So `ta_create_approval` triggers identity only when its agent has no upstream feeder.
_PATIENT_REQUIRED_IF_LEAD_TASKS = frozenset({"ta_create_approval"})


def _agents_requiring_patient(agents: list[dict], edges: list[dict] | None = None) -> list[str]:
    """Agent ids that need a resolved INCOMING patient identity. Keys off the tasks
    actually chosen at stage 3 -- so it no longer depends on the LLM setting a task_type
    label. Bed reservation counts only when the agent is a graph entry point (no non-PV
    feeder); downstream reservations are internal flow or inherit identity from an
    upstream ER/ICU-ranking node."""
    fed = {
        e.get("target") for e in (edges or [])
        if e.get("source") not in ("", None, _PV_AGENT_ID) and e.get("source") != e.get("target")
    }
    out = []
    for a in agents:
        aid = a["id"]
        selected = {t.get("id")
                    for sa in a.get("sub_agents", [])
                    for t in sa.get("tasks", [])}
        if selected & _PATIENT_REQUIRED_TASKS:
            out.append(aid)
        elif (selected & _PATIENT_REQUIRED_IF_LEAD_TASKS) and aid not in fed:
            out.append(aid)  # standalone reservation for a genuine incoming patient
    return out


def _strip_patient_verification(pipeline: dict) -> None:
    """Remove patient_verification_agent and every edge touching it."""
    pipeline["agents"] = [a for a in pipeline.get("agents", []) if a.get("id") != _PV_AGENT_ID]
    pipeline["edges"] = [
        e for e in pipeline.get("edges", [])
        if e.get("source") != _PV_AGENT_ID and e.get("target") != _PV_AGENT_ID
    ]


# Gate: even when a patient-required task is selected, PV only makes sense when the goal
# concerns ONE specific incoming/arriving individual whose identity must be resolved --
# not a population-level / forecast / operational question. A cheap Haiku call decides;
# a deterministic signal is the fallback when the LLM is unavailable.
_INCOMING_PATIENT_CLASSIFIER = """\
A hospital planning system decides whether to run a patient-identification step. That
step only makes sense when the request concerns ONE specific incoming or arriving
individual patient whose identity must be resolved -- e.g. an ambulance is bringing a
named patient, "register the patient at the door", or a mobile number is given.

It must NOT run for population-level, forecast, or operational questions about the ward,
ED, or hospital as a whole -- e.g. "can the ED cope tonight", "forecast surge", "how many
boarders", "assess staffing", "which wards will breach".

Request:
{goal}

Does this request concern a specific incoming/arriving individual patient who must be
identified? Answer with only "yes" or "no"."""

# Strong deterministic signals for the LLM-unavailable fallback. Conservative by design:
# default to NOT injecting unless a mobile number or explicit singular-arrival phrasing
# is present, and let clearly-aggregate phrasing veto.
_INCOMING_PHRASES = (
    "incoming patient", "arriving patient", "patient arriving", "en route",
    "bringing in", "at the door", "register the patient", "identify the patient",
    "admit the patient", "this incoming",
)
_AGGREGATE_PHRASES = (
    "forecast", "how many", "cope", "walk-in volume", "walk-ins", "surge",
    "staffing", "which wards", "capacity", "boarders", "hospital-wide", "assess whether",
)
_MOBILE_RE = re.compile(r"(?<!\d)\+?\d[\d\s\-]{7,}\d(?!\d)")


def _incoming_patient_heuristic(goal: str) -> bool:
    g = (goal or "").lower()
    if _MOBILE_RE.search(g):
        return True
    if any(p in g for p in _AGGREGATE_PHRASES):
        return False
    return any(p in g for p in _INCOMING_PHRASES)


async def _goal_is_incoming_individual(goal: str, session_id: str = "") -> bool:
    """True when the goal concerns a specific incoming/arriving individual patient who
    must be identified (mobile -> token). Gates patient_verification injection so PV
    never attaches to population-level / forecast / operational goals. Haiku decides;
    falls back to a deterministic signal on any LLM error."""
    if not (goal or "").strip():
        return False
    try:
        reply = await llm_chat(
            user=_INCOMING_PATIENT_CLASSIFIER.format(goal=goal),
            max_tokens=3, tier="fast",
        )
        decision = reply.strip().lower().startswith("y")
        logger.info("  ↳ PV gate (haiku)  incoming_individual=%s  session=%s", decision, session_id)
        return decision
    except Exception:  # noqa: BLE001
        decision = _incoming_patient_heuristic(goal)
        logger.warning("  ↳ PV gate (haiku) failed -- heuristic=%s  session=%s",
                       decision, session_id, exc_info=True)
        return decision


async def _inject_patient_verification(pipeline: dict, session_id: str = "", goal: str = "") -> None:
    """Stage 3b: ensure patient_verification_agent leads every agent whose selected
    tasks require a resolved patient identity. Mutates ``pipeline`` in place.

    User override: if the user removed patient_verification via an edit, the session
    carries a suppression flag. Honor it -- strip PV and never re-add -- so the removal
    sticks across reorchestrations instead of the safety net silently forcing it back.
    """
    if session_id:
        from cache import redis as cache
        if await cache.get(f"session:{session_id}:pv_suppressed"):
            _strip_patient_verification(pipeline)
            logger.info("  ↳ patient_verification suppressed by user  session=%s", session_id)
            return

    agents = pipeline.get("agents", [])
    has_pv = any(a["id"] == _PV_AGENT_ID for a in agents)
    consumers = _agents_requiring_patient(agents, pipeline.get("edges", []))
    # Nothing requires identity AND the LLM didn't add PV on its own -> nothing to do.
    # When PV IS present (LLM-added) we must still run the goal gate below, otherwise a
    # population-level plan with no trigger task keeps a spurious PV that pauses the run.
    if not consumers and not has_pv:
        return

    # Only resolve identity when the goal is actually about a specific incoming/arriving
    # individual. Otherwise PV injects a spurious prerequisite that PAUSES the run asking
    # for a mobile number the goal never gave (require_patients interrupts on an
    # unidentified session). Strip any PV an earlier pass / the LLM added, and don't inject.
    if not await _goal_is_incoming_individual(goal, session_id):
        _strip_patient_verification(pipeline)
        logger.info("  ↳ patient_verification skipped -- goal is population-level, not an "
                    "incoming individual  session=%s  consumers=%s", session_id, consumers)
        return

    if not any(a["id"] == _PV_AGENT_ID for a in agents):
        try:
            available, sub_agents, _ind, _ids = await _fetch_registry()
        except Exception:  # noqa: BLE001
            available, sub_agents = AVAILABLE_AGENTS, SUB_AGENTS
        pv_def = next((a for a in available if a["id"] == _PV_AGENT_ID),
                      {"id": _PV_AGENT_ID, "label": "Patient Verification", "color": "#0ea5e9"})
        # Attach the catalog sub-agents/tasks so materialize_preplans builds a real
        # "run" plan (not the skip-sentinel it emits for an empty sub_agents list),
        # preserving the unknown-patient flag/registration steps and the canvas view.
        pv_catalog = sub_agents.get(_PV_AGENT_ID) or []
        agents.append({
            **pv_def,
            "role": "Establish incoming patient identity (token + vitals) before downstream clinical agents act",
            "sub_agents": [
                {"id": sa.id, "label": sa.label,
                 "tasks": [{"id": t.id, "label": t.label, "condition": None, "outputs": t.outputs}
                           for t in sa.tasks]}
                for sa in pv_catalog
            ],
        })
        logger.info("  ↳ injecting patient_verification_agent (consumers=%s)", consumers)

    # It must lead: strip any incoming edges, then add an unconditional edge to entry-point
    # consumers only. A consumer is an "entry point" if no other consumer already feeds it —
    # e.g. if er_agent → icu_agent exists, icu_agent gets identity via er_agent and doesn't
    # need a direct patient_verification edge (which would create a spurious parallel path).
    edges = [e for e in pipeline.get("edges", []) if e.get("target") != _PV_AGENT_ID]
    consumer_set = set(consumers)
    already_covered = {
        e["target"] for e in edges
        if e.get("source") in consumer_set and e.get("target") in consumer_set
    }
    existing = {(e.get("source"), e.get("target")) for e in edges}
    for c in [c for c in consumers if c not in already_covered]:
        if (_PV_AGENT_ID, c) not in existing:
            edges.append({"source": _PV_AGENT_ID, "target": c})
            existing.add((_PV_AGENT_ID, c))
    pipeline["edges"] = edges


# -- Layer 2: deterministic condition injection --------------------------------
# Safety net: if the LLM generated the right agent graph but forgot conditions,
# these rules patch the known domain invariants without overwriting explicit ones.

_CANONICAL_CONDITIONS: list[dict] = [
    {"source": "icu_agent",       "target": "discharge_agent", "condition": "icu_full",                 "condition_label": "if ICU full"},
    # G40: do NOT auto-inject a discharge-candidate gate onto revenue_agent. Revenue
    # reports STANDING hospital-wide metrics that must surface even when there are no
    # discharge candidates; gating it here suppressed them (e.g. a CEO dashboard). The
    # revenue->discharge_ready rules were removed for this reason.
]

# G40: revenue / billing / notification report STANDING metrics/results, so an edge
# into them must never be gated on a candidate-existence condition.
_STANDING_REPORT_TARGETS = {"revenue_agent", "billing_agent", "notification_agent"}


def _is_candidate_existence_cond(cond: str) -> bool:
    """True for a gate that keys off whether a candidate / recommendation set is
    non-empty -- has_discharge_candidates, step_down_candidates>0, escalation_candidates,
    diversion_recommended, candidates_found, discharge_ready, etc. These wrongly suppress
    standing revenue/billing/notification output when the set happens to be empty (G40)."""
    c = (cond or "").strip().lower()
    if not c:
        return False
    return ("candidate" in c) or ("diversion_recommended" in c) or c in {"discharge_ready", "discharge_not_ready"}


def _apply_canonical_conditions(agents: list[dict], edges: list[dict]) -> list[dict]:
    """Inject known domain conditions onto edges that are missing them."""
    agent_base_ids = {_agent_base_id(a["id"]) for a in agents}
    result = []
    for edge in edges:
        patched = dict(edge)
        if not patched.get("condition"):
            src_base = _agent_base_id(patched.get("source", ""))
            tgt_base = _agent_base_id(patched.get("target", ""))
            for rule in _CANONICAL_CONDITIONS:
                if src_base == rule["source"] and tgt_base == rule["target"]:
                    # Skip rule if it references an agent not present in the pipeline
                    if rule.get("condition_source") and rule["condition_source"] not in agent_base_ids:
                        break
                    patched["condition"]       = rule["condition"]
                    patched["condition_label"] = rule["condition_label"]
                    if "condition_source" in rule:
                        patched["condition_source"] = rule["condition_source"]
                    logger.info(
                        "  ↳ canonical condition injected: %s->%s [%s]",
                        patched.get("source"), patched.get("target"), rule["condition"],
                    )
                    break
        result.append(patched)
    return result


def _scope_icu_full_discharge_gate(edges: list[dict]) -> list[dict]:
    """G39: the icu_full gate on icu_agent -> discharge_agent is only valid as one arm
    of the capacity-relief branch (discharge to free beds ONLY if ICU is full), whose
    other arm is icu_agent -> bed_agent (condition icu_not_full). When that counterpart
    arm is absent, discharge is the goal itself or a reporting need and must run
    regardless of ICU fullness -- so the spurious icu_full gate is removed (otherwise
    discharge is suppressed whenever the ICU is not full, e.g. a 14-day ICU discharge,
    an AMA discharge, or a census/briefing)."""
    has_icu_not_full_branch = any(
        _agent_base_id(e.get("source", "")) == "icu_agent"
        and (e.get("condition") or "").strip().lower() == "icu_not_full"
        for e in edges
    )
    if has_icu_not_full_branch:
        return edges
    for e in edges:
        if (_agent_base_id(e.get("source", "")) == "icu_agent"
                and _agent_base_id(e.get("target", "")) == "discharge_agent"
                and (e.get("condition") or "").strip().lower() == "icu_full"):
            e.pop("condition", None)
            e.pop("condition_label", None)
            e.pop("condition_source", None)
            logger.info("  ↳ G39: stripped spurious icu_full gate on icu_agent->discharge_agent (no capacity-relief branch)")
    return edges


def _collapse_discharge_fanout(edges: list[dict]) -> list[dict]:
    """When discharge_agent fans out to multiple agents on the same condition,
    keep the first conditional edge and chain subsequent ones off that target.

    e.g. discharge_agent -[has_discharge_candidates]-> appointment_agent
         discharge_agent -[has_discharge_candidates]-> revenue_agent
    becomes:
         discharge_agent -[has_discharge_candidates]-> appointment_agent
         appointment_agent -> revenue_agent  (unconditional)

    Single-edge cases are untouched.
    """
    DISCHARGE_CONDITIONS = {"has_discharge_candidates", "discharge_ready"}
    first_target: str | None = None
    result = []
    for edge in edges:
        if _agent_base_id(edge.get("source", "")) == "discharge_agent" and edge.get("condition") in DISCHARGE_CONDITIONS:
            if first_target is None:
                first_target = edge["target"]
                result.append(edge)
            else:
                chained = {k: v for k, v in edge.items() if k not in ("condition", "condition_label", "condition_source")}
                chained["source"] = first_target
                result.append(chained)
                logger.info("  ↳ discharge fanout collapsed: discharge_agent->%s chained after %s", edge["target"], first_target)
        else:
            result.append(edge)
    return result


# -- 3-stage planning (used by the LangGraph planning graph) -------------------
# Planning is split into three focused LLM passes -- (1) agents+edges,
# (2) sub-agents per agent, (3) tasks per sub-agent -- which produces materially
# better plans than a single mega-prompt. Each reuses the existing helpers. Stage 3's
# per-task output is already preplan-shaped ({condition, label, outputs}) so
# services.materializer can bind it to execution directly.


async def generate_pipeline_staged(
    goal: str, constraints: str = "", prior_plan: dict | None = None, session_id: str = "",
) -> dict:
    """Synchronous staged pipeline generation -- the newer multi-pass planner.

    Runs the same focused passes the LangGraph planning graph runs at its
    plan_agents -> plan_subagents -> plan_tasks -> validate_plan nodes, in order,
    and returns the cleaned pipeline -- WITHOUT the durable checkpoint / human-
    approval interrupt machinery. Use this for callers that need a finished
    pipeline in a single await (e.g. the reorchestrate endpoint).

    `prior_plan` makes the run a REVISION of an existing plan instead of a fresh
    build: stages 1-2 are shown the current agents/edges/sub-agents and told to
    apply the feedback incrementally, so untouched parts of the plan survive the
    round (the caller is responsible for replaying earlier feedback in `goal`).

    Returns the same pipeline shape the planning graph persists at validate_plan.
    """
    logger.info('-> generating pipeline (staged)  goal="%s"  revision=%s',
                goal[:80], bool(prior_plan and prior_plan.get("agents")))
    pipeline = await generate_agents_and_edges(goal, constraints, prior_plan=prior_plan)
    pipeline = await select_pipeline_subagents(pipeline, goal, prior_plan=prior_plan)
    pipeline = await plan_pipeline_tasks(pipeline, goal)
    pipeline = await validate_and_dedupe_plan(pipeline, goal, session_id=session_id)
    return pipeline


# -- Revision context (reorchestration) ---------------------------------------
# Reorchestration replans from scratch, which loses whatever the previous round(s)
# already got right. These helpers hand the current plan back to the planner so a
# round of feedback is applied AS A DIFF: keep what the feedback doesn't mention,
# change only what it does.

def goal_with_feedback_history(goal: str, history: list[str]) -> str:
    """Original goal + every reorchestration feedback so far, oldest first.

    Each reorchestration round is a full replan whose only memory of earlier rounds is
    this string, so the whole history travels with it and later entries are declared
    authoritative -- otherwise round 2 ("add billing agent") regenerates from the raw
    query and undoes round 1 ("now the ICU is at 57%")."""
    history = [fb.strip() for fb in history if fb and fb.strip()]
    if not history:
        return goal
    if len(history) == 1:
        return f"{goal}\n\nUser feedback on previous plan: {history[0]}"
    numbered = "\n".join(f"{i}. {fb}" for i, fb in enumerate(history, 1))
    return (
        f"{goal}\n\n"
        "User feedback across successive reorchestration rounds, OLDEST FIRST. Apply ALL "
        "of it cumulatively: every revision still holds unless a later one contradicts it, "
        "in which case the LATER entry wins. Never fall back to a value from the original "
        "query that a later entry replaced.\n"
        f"{numbered}\n"
        f"The most recent request is #{len(history)} -- make sure the plan reflects it."
    )


_REVISION_HEADER = """
=== THIS IS A REVISION OF AN EXISTING PLAN ===
The plan below is the CURRENT plan the user is looking at. The feedback in the goal
above (all of it, in order -- later entries win on conflict) has to be applied to
THIS plan, not to a blank slate.

Rules:
- Keep every agent and edge the feedback does not talk about, with the SAME id,
  label, role, task_type and bed_limit as below.
- Change only what the feedback asks for: add / remove / reorder agents, or reword
  the role of an agent whose facts the feedback updates.
- Never revert a value that earlier feedback already changed (numbers, counts,
  thresholds, wards, dates) back to what the original query said.
"""


def _revision_block(prior_plan: dict | None) -> str:
    """Stage-1 revision context: the current agent graph, verbatim enough to keep."""
    agents = (prior_plan or {}).get("agents") or []
    if not agents:
        return ""
    lines = []
    for a in agents:
        bits = [f'id={a.get("id")}', f'label={a.get("label")!r}', f'role={a.get("role")!r}']
        if a.get("task_type"):
            bits.append(f'task_type={a["task_type"]}')
        if a.get("bed_limit") not in (None, ""):
            bits.append(f'bed_limit={a["bed_limit"]}')
        lines.append("  - " + "  ".join(bits))
    edges = [
        "  - {source} -> {target}{cond}".format(
            source=e.get("source"), target=e.get("target"),
            cond=f'  [{e["condition"]}]' if e.get("condition") else "",
        )
        for e in (prior_plan or {}).get("edges") or []
    ]
    out = _REVISION_HEADER + "\nCurrent agents:\n" + "\n".join(lines)
    if edges:
        out += "\nCurrent edges:\n" + "\n".join(edges)
    # Being asked to diff a plan makes the model want to narrate the diff first; this
    # block is the LAST thing it reads, so restate the output contract here.
    out += ("\n\nReturn ONLY the JSON object for the FULL revised plan (every agent that "
            "survives, not just the changed ones). No preamble, no explanation, no "
            "commentary before or after the JSON.")
    return out


def _prior_subagents_for(prior_plan: dict | None, agent_id: str) -> str:
    """Stage-2 revision context: what this agent currently runs, so an unrelated
    round of feedback ('add billing agent') does not reshuffle it."""
    for a in (prior_plan or {}).get("agents") or []:
        if a.get("id") != agent_id:
            continue
        subs = a.get("sub_agents") or []
        if not subs:
            return ""
        cur = [
            "  - {id}{cond}  subgoal={sg!r}".format(
                id=sa.get("id"),
                cond=f'  [{sa["condition"]}]' if sa.get("condition") else "",
                sg=sa.get("subgoal", ""),
            )
            for sa in subs
        ]
        return (
            f"\n\nCurrent sub-agent selection for {agent_id} (this is a REVISION -- keep this "
            f"selection and order unless the feedback asks otherwise; only restate a subgoal "
            f"when the feedback changes its facts):\n" + "\n".join(cur)
        )
    return ""

USER_TEMPLATE_AGENTS = """Hospital operational goal: "{goal}"
Constraints: "{constraints}"

Plan ONLY the agent graph -- which agents run and how they connect. Do NOT include
sub_agents or tasks (those are planned in later stages).

Execution strategies govern how agents coordinate when several run in the SAME level
(a level = agents with no dependency between them, so they run together). Choose per agent
from this list:
{strategy_catalogue}
On each agent, set "strategy" to the id whose "use when" fits THAT agent's role in its
level, or omit it to inherit the default (the strategy the catalogue marks as the default).
Decide strictly by matching each strategy's "use when" to the situation -- do not assume
anything beyond what the catalogue states. Most agents just inherit the default; only tag an
agent when its own "use when" condition is clearly met (e.g. it contends with a sibling in
the same level for one scarce unit).

Return ONLY valid JSON, no markdown:
{{
  "understood_goal": "one sentence summary",
  "priority": "urgent | high | normal",
  "agents": [
    {{"id": "<agent_id from available list>", "label": "<label>", "color": "<color>",
      "role": "what this agent does in this pipeline",
      "strategy": "<one strategy id from the list above whose 'use when' fits THIS agent, or omit to inherit the default>",
      "task_type": "<bed_agent: availability_check|bed_reservation|bed_cleaning; icu_agent: capacity_check|full_analysis; billing_agent: patient_billing|initiate_billing; omit otherwise>",
      "bed_limit": "<integer if the user gave an explicit bed/patient count, else omit>"}}
  ],
  "edges": [
    {{"source": "<agent_id>", "target": "<agent_id>", "condition": "<optional>", "condition_label": "<optional>"}}
  ],
  "prefetch": [
    {{"agent_id": "<agent_id>", "subagent_id": "<subagent_id>", "task_id": "<prefetch-eligible task_id>"}}
  ]
}}"""


async def generate_agents_and_edges(
    goal: str, constraints: str = "", prior_plan: dict | None = None,
) -> dict:
    """Stage 1/3: plan the agent graph (agents + edges + prefetch) only.

    Uses AGENT_GRAPH_SYSTEM_PROMPT and the agent-level repair layers
    (_inject_missing_agents, _apply_canonical_conditions, dangling/disconnected
    cleanup, prefetch normalization). Sub-agents/tasks are added in stages 2-3.

    `prior_plan` (reorchestration) turns this into an incremental revision of that
    plan rather than a fresh build -- see _revision_block.
    """
    logger.info('-> [stage1] agents+edges  goal="%s"', goal[:80])
    available_agents, sub_agents, independent_prefetch_tasks, _sa_task_ids = await _fetch_registry()
    system_prompt = _build_system_prompt(
        available_agents, independent_prefetch_tasks,
        base_prompt=AGENT_GRAPH_SYSTEM_PROMPT,
    )

    _user_content = USER_TEMPLATE_AGENTS.format(
        goal=goal, constraints=constraints or "none",
        strategy_catalogue=strategy_catalogue_text())
    _user_content += _revision_block(prior_plan)
    _dump_prompt("AGENTS + EDGES BUILD (stage 1)", system_prompt, _user_content)

    last_exc: Exception | None = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            text = await llm_chat(
                system=system_prompt,
                user=_user_content,
                max_tokens=2048,
                # deterministic: the same goal must yield the same agent graph -- incl.
                # whether patient_verification leads (the discharge/billing "one named
                # patient?" call). temp>0 here was the source of the flip-flopping.
                temperature=0.0,
                tier="quality",
            )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning("LLM overloaded [stage1] (%d/4) -- retrying", attempt + 1)
    else:
        raise last_exc  # type: ignore[misc]
    text = _json_object(text)
    pipeline = json.loads(text)
    pipeline.setdefault("agents", [])
    pipeline.setdefault("edges", [])
    # Execution strategy is now chosen PER AGENT (the builder resolves it per level);
    # the JSON's default: true strategy is the global default, so no pipeline-level
    # strategy is required. Drop any per-agent strategy the LLM invented that isn't a
    # real id -- an absent/invalid tag simply inherits the default at build time. A
    # top-level pipeline["strategy"], if the LLM still emits one or an old snapshot
    # carries one, is left untouched and honoured by the builder as the fallback default.
    for a in pipeline["agents"]:
        if "strategy" in a and not is_valid_strategy(a.get("strategy")):
            a.pop("strategy", None)
        a.pop("sub_agents", None)  # stages 2-3 own these

    # Agent-level edge fixes
    for edge in pipeline["edges"]:
        # G40: revenue / billing / notification report STANDING metrics/results and
        # must not be gated on a candidate-existence condition (has_discharge_candidates
        # / step_down_candidates>0 / diversion_recommended). Gating them drops the
        # standing output exactly when no candidate exists (e.g. a CEO dashboard losing
        # revenue metrics). Strip the gate so the branch always runs and reports what it has.
        if (_agent_base_id(edge.get("target", "")) in _STANDING_REPORT_TARGETS
                and _is_candidate_existence_cond(edge.get("condition", ""))):
            edge.pop("condition", None)
            edge.pop("condition_label", None)
            edge.pop("condition_source", None)
        if edge.get("source") == "icu_agent" and "bed_agent" in edge.get("target", ""):
            edge.pop("condition", None)
            edge.pop("condition_label", None)
        if edge.get("source") == "discharge_agent" and "bed_agent" in edge.get("target", ""):
            edge.pop("condition", None)
            edge.pop("condition_label", None)

    # Prefetch normalize (subagent/task selection filters happen in stages 2-3) + fallback.
    # Stage 1 has no sub-agents/tasks yet, so we can only dedup + drop non-eligible tasks here.
    if pipeline.get("prefetch"):
        seen: set = set()
        normalized = []
        for item in pipeline["prefetch"]:
            said, tid = item.get("subagent_id"), item.get("task_id")
            if tid not in PREFETCH_TASK_RUNNERS:
                continue
            key = (_agent_base_id(item["agent_id"]), said, tid)
            if key not in seen:
                seen.add(key)
                normalized.append({"agent_id": _agent_base_id(item["agent_id"]), "subagent_id": said, "task_id": tid})
        pipeline["prefetch"] = normalized
    if not pipeline.get("prefetch"):
        present = {_agent_base_id(a["id"]) for a in pipeline["agents"]}
        pipeline["prefetch"] = [
            {"agent_id": p["agent_id"], "subagent_id": p["subagent_id"], "task_id": p["task_id"]}
            for p in INDEPENDENT_PREFETCH_TASKS
            if p["agent_id"] in present
        ]

    pipeline["agents"], pipeline["edges"] = _inject_missing_agents(
        pipeline["agents"], pipeline["edges"],
        available_agents=available_agents, sub_agents=sub_agents)
    for a in pipeline["agents"]:
        a.pop("sub_agents", None)  # _inject adds catalog sub_agents -- stage 2 re-selects
    # patient_verification is injected at stage 3b (validate_and_dedupe_plan), once the
    # selected tasks exist -- the trigger is the chosen task, not a stage-1 task_type guess.
    pipeline["edges"] = _apply_canonical_conditions(pipeline["agents"], pipeline["edges"])
    pipeline["edges"] = _scope_icu_full_discharge_gate(pipeline["edges"])
    pipeline["edges"] = _collapse_discharge_fanout(pipeline["edges"])

    known = {a["id"] for a in pipeline["agents"]}
    pipeline["edges"] = [e for e in pipeline["edges"] if e["source"] in known and e["target"] in known]
    if len(pipeline["agents"]) > 1:
        wired = {e["source"] for e in pipeline["edges"]} | {e["target"] for e in pipeline["edges"]}
        pipeline["agents"] = [a for a in pipeline["agents"] if a["id"] in wired]
    pipeline["prefetch"] = [
        p for p in pipeline.get("prefetch", [])
        if any(_agent_base_id(a["id"]) == p["agent_id"] for a in pipeline["agents"])
    ]
    try:
        from workflows.graph.builder import _plan_levels
        _lv, _ = _plan_levels(pipeline["agents"], pipeline["edges"])
        _lead = _lv[0] if _lv else []
    except Exception:
        _lead = "(unknown)"
    logger.info(
        "<- [stage1] lead=%s agents=%s edges=%d",
        _lead, [a["id"] for a in pipeline["agents"]], len(pipeline["edges"]),
    )
    return pipeline


# Read-only task_types must not pull in write/action sub-agents (reserve, transfer,
# dispatch, cleaning). Whitelist the read-only sub-agents per (agent_base, task_type);
# when an agent carries that task_type, drop everything outside the whitelist. Mirrors
# the revenue/bed special-casing in select_pipeline_subagents and the bed-agent filter
# the web UI already applies. Only read-only task_types appear here -- write task_types
# (bed_reservation, full_analysis, ...) are intentionally absent so they keep all sub-agents.
_READONLY_TASK_TYPE_SUBAGENTS: dict[tuple[str, str], set[str]] = {
    ("icu_agent", "capacity_check"):     {"sa_icu_census"},
    ("bed_agent", "availability_check"): {"sa_bed_availability", "sa_bed_ranking"},
    # G22: snapshot/dashboard/report goals — suppress all write/action sub-agents.
    ("discharge_agent", "snapshot"): {"sa_discharge_ready", "sa_discharge_retrospective"},
    ("icu_agent",       "snapshot"): {"sa_icu_census"},
    ("staff_agent",     "snapshot"): {"sa_ratio_monitor"},
    ("revenue_agent",   "snapshot"): {"sa_rev_optimization", "sa_rev_denial_prevention"},
}


def _pipeline_context_for(pipeline: dict, exclude_agent_id: str) -> str:
    # Stage 2: neighbours' sub-agents aren't selected yet, so role-level detail only.
    return build_graph_context(pipeline, exclude_agent_id, include_subagents=False)


async def select_pipeline_subagents(
    pipeline: dict, goal: str, prior_plan: dict | None = None,
) -> dict:
    """Stage 2/3: pick which sub-agents run per agent (reuses select_subagents).

    Attaches sub_agents:[{id,label,role}] (no tasks yet). Applies the multi-instance
    (sa_dirty_bed_recovery only on :after_discharge) and revenue sub-agent rules.
    Runs the per-agent LLM calls concurrently.

    `prior_plan` (reorchestration) shows each agent its current sub-agent selection so
    an unrelated round of feedback does not reshuffle agents it never mentioned.
    """
    _avail, sub_agents, _ind, _ids = await _fetch_registry()

    async def _one(agent: dict) -> None:
        base = _agent_base_id(agent["id"])
        catalog = sub_agents.get(base)
        if not catalog:
            return  # DB-registry agent -- leave for the body's DB planning
        agent_goal = goal + _prior_subagents_for(prior_plan, agent["id"])
        selected, subgoals, conditions = await select_subagents(
            base, catalog, agent_goal, _pipeline_context_for(pipeline, agent["id"]))
        cat_map = {sa.id: sa for sa in catalog}
        agent["sub_agents"] = [
            {"id": sid, "label": cat_map[sid].label, "role": cat_map[sid].label,
             "subgoal": subgoals.get(sid, ""),
             **({"condition": conditions[sid]} if sid in conditions else {})}
            for sid in selected if sid in cat_map
        ]

    await asyncio.gather(*[_one(a) for a in pipeline.get("agents", [])])

    # Sub-agent selection is the LLM's job (stage 2) -- no hardcoded post-filtering.
    # Tune which sub-agents appear via their registry descriptions / select_subagents.txt,
    # not by force-dropping or force-keeping them here.

    # Read-only task_types (capacity_check, availability_check) verify state -- they must
    # not run write/action sub-agents. Clamp to the whitelist so e.g. an ICU capacity_check
    # keeps only the census and never pulls in the reserve/overflow transfer sub-agent.
    for a in pipeline.get("agents", []):
        allowed = _READONLY_TASK_TYPE_SUBAGENTS.get((_agent_base_id(a["id"]), a.get("task_type") or ""))
        if not allowed:
            continue
        kept = [sa for sa in a.get("sub_agents", []) if sa["id"] in allowed]
        dropped = [sa["id"] for sa in a.get("sub_agents", []) if sa["id"] not in allowed]
        if dropped and kept:  # never blank the agent on an id mismatch -- keep originals
            a["sub_agents"] = kept
            logger.info("stage2: %s task_type=%s read-only -- dropped write sub-agents %s",
                        a["id"], a.get("task_type"), dropped)

    selected_sa = {sa["id"] for a in pipeline.get("agents", []) for sa in a.get("sub_agents", [])}
    pipeline["prefetch"] = [p for p in pipeline.get("prefetch", []) if p["subagent_id"] in selected_sa]
    logger.info("<- [stage2] sub-agents=%s",
                {a["id"]: [sa["id"] for sa in a.get("sub_agents", [])] for a in pipeline.get("agents", [])})
    return pipeline


async def plan_pipeline_tasks(pipeline: dict, goal: str) -> dict:
    """Stage 3/3: pick tasks + conditions per sub-agent (reuses plan_subagent_tasks).

    Attaches preplan-shaped tasks ({id,label,condition,outputs}) to sub_agents[].tasks.
    Runs the per-sub-agent LLM calls concurrently.
    """
    _avail, sub_agents, _ind, _ids = await _fetch_registry()

    async def _one(agent: dict, sa: dict) -> None:
        base = _agent_base_id(agent["id"])
        catalog = sub_agents.get(base) or []
        cat_sa = next((c for c in catalog if c.id == sa["id"]), None)
        available = ([{"id": t.id, "label": t.label, "outputs": t.outputs} for t in cat_sa.tasks]
                     if cat_sa else [])
        plan = await plan_subagent_tasks(PlanSubagentInput(
            agent_id=base, subagent_id=sa["id"], available_tasks=available, goal=goal,
            subgoal=sa.get("subgoal", "")))
        sa["tasks"] = [
            {"id": tid, "label": entry.get("label", tid),
             "condition": entry.get("condition"), "outputs": entry.get("outputs", [])}
            for tid, entry in (plan or {}).items()
        ]

    jobs = [_one(a, sa) for a in pipeline.get("agents", []) for sa in a.get("sub_agents", [])]
    if jobs:
        await asyncio.gather(*jobs)

    # Final prefetch filter: keep only entries whose task was actually selected
    # (per-task analogue of the post-stage-2 subagent filter). Tasks exist now.
    selected_tasks: set[tuple] = {
        (sa["id"], t["id"])
        for a in pipeline.get("agents", [])
        for sa in a.get("sub_agents", [])
        for t in sa.get("tasks", [])
    }
    pipeline["prefetch"] = [
        p for p in pipeline.get("prefetch", [])
        if (p.get("subagent_id"), p.get("task_id")) in selected_tasks
    ]
    # Drop subagents that ended up with no tasks -- they add no work to the plan.
    for agent in pipeline.get("agents", []):
        before = len(agent.get("sub_agents", []))
        agent["sub_agents"] = [sa for sa in agent.get("sub_agents", []) if sa.get("tasks")]
        dropped = before - len(agent["sub_agents"])
        if dropped:
            logger.info("stage3: dropped %d empty sub-agent(s) from %s", dropped, agent["id"])

    logger.info("<- [stage3] tasks bound for %d sub-agent(s)", len(jobs))
    return pipeline


# -- Plan validation / de-duplication (stage 3b) -------------------------------
# The LLM planner frequently emits redundant structure: the same agent edge twice,
# two same-source/same-target branches whose conditions mean the same thing, or a
# task repeated across sub-agents. This step cleans the fully-built plan before it
# is shown for approval. Two passes:
#   A. deterministic -- exact/structural duplicates (pure Python, never fails)
#   B. semantic       -- Haiku merges branches worded differently but equivalent
# Both are best-effort: any failure falls back to the best plan so far.

_VALIDATE_SYSTEM = _load_prompt("validate_plan.txt")

_VALIDATE_USER = """Goal: {goal}

Agent edges:
{edges}

Sub-agent task_edges:
{task_edges}

Return only the JSON of redundant branches to merge."""


def _edge_key(e: dict) -> tuple:
    return (e.get("source"), e.get("target"), e.get("condition"))


def _dedupe_plan_structural(pipeline: dict) -> dict:
    """Pass A: remove exact/structural duplicates. Pure Python, never raises."""
    agents = pipeline.get("agents", [])

    # 1) Agent edges: drop exact (source,target,condition) dupes, then collapse
    #    multiple edges sharing the same (source,target) to one -- keeping the
    #    condition-bearing, richest-metadata edge. Different targets are kept.
    seen_edges: set[tuple] = set()
    by_pair: dict[tuple, dict] = {}
    pair_order: list[tuple] = []
    for e in pipeline.get("edges", []):
        key = _edge_key(e)
        if key in seen_edges:
            logger.info("  ↳ dedupe: dropped duplicate edge %s->%s [%s]",
                        e.get("source"), e.get("target"), e.get("condition"))
            continue
        seen_edges.add(key)
        pair = (e.get("source"), e.get("target"))
        prev = by_pair.get(pair)
        if prev is None:
            by_pair[pair] = e
            pair_order.append(pair)
        else:
            # collapse: prefer the edge carrying a condition / richer metadata
            richer = _richer_edge(prev, e)
            if richer is not prev:
                by_pair[pair] = richer
            logger.info("  ↳ dedupe: collapsed sibling edges %s->%s", *pair)
    pipeline["edges"] = [by_pair[p] for p in pair_order]

    # 2) Per sub-agent: dedupe tasks by id and task_edges by (source,target,cond).
    # 3) Per agent across sub-agents: a task id appearing in >1 sub-agent is a
    #    repeated step -- keep only its first occurrence.
    for agent in agents:
        seen_tasks_agent: set[str] = set()
        for sa in agent.get("sub_agents", []):
            kept_tasks = []
            seen_in_sa: set[str] = set()
            for t in sa.get("tasks", []):
                tid = t if isinstance(t, str) else t.get("id", "")
                if tid in seen_in_sa or tid in seen_tasks_agent:
                    logger.info("  ↳ dedupe: dropped repeated task %s in %s/%s",
                                tid, agent.get("id"), sa.get("id"))
                    continue
                seen_in_sa.add(tid)
                seen_tasks_agent.add(tid)
                kept_tasks.append(t)
            sa["tasks"] = kept_tasks

            seen_te: set[tuple] = set()
            kept_te = []
            for te in sa.get("task_edges", []):
                key = _edge_key(te)
                if key in seen_te:
                    logger.info("  ↳ dedupe: dropped duplicate task_edge %s->%s in %s",
                                te.get("source"), te.get("target"), sa.get("id"))
                    continue
                seen_te.add(key)
                kept_te.append(te)
            if "task_edges" in sa:
                sa["task_edges"] = kept_te
    return pipeline


def _richer_edge(a: dict, b: dict) -> dict:
    """Pick the more informative of two edges sharing a (source,target) pair."""
    def score(e: dict) -> int:
        return (bool(e.get("condition")) + bool(e.get("condition_label"))
                + bool(e.get("condition_source")))
    return b if score(b) > score(a) else a


def _apply_merge_findings(pipeline: dict, findings: dict) -> None:
    """Apply Haiku's merge findings deterministically. Unmatched refs are no-ops."""
    # Agent edges
    remove_edges = {
        _edge_key(r)
        for m in findings.get("merge_edges", []) or []
        for r in (m.get("remove") or [])
    }
    if remove_edges:
        keep = []
        for e in pipeline.get("edges", []):
            if _edge_key(e) in remove_edges:
                logger.info("  ↳ semantic-merge: removed edge %s->%s [%s]",
                            e.get("source"), e.get("target"), e.get("condition"))
                continue
            keep.append(e)
        pipeline["edges"] = keep
    for m in findings.get("merge_edges", []) or []:
        if not m.get("merged_label"):
            continue
        kk = _edge_key(m.get("keep", {}))
        for e in pipeline.get("edges", []):
            if _edge_key(e) == kk:
                e["condition_label"] = m["merged_label"]

    # Per-sub-agent task_edges
    for m in findings.get("merge_task_edges", []) or []:
        aid, said = m.get("agent_id"), m.get("subagent_id")
        remove = {_edge_key(r) for r in (m.get("remove") or [])}
        if not remove:
            continue
        for agent in pipeline.get("agents", []):
            if agent.get("id") != aid:
                continue
            for sa in agent.get("sub_agents", []):
                if sa.get("id") != said or "task_edges" not in sa:
                    continue
                kept = []
                for te in sa["task_edges"]:
                    if _edge_key(te) in remove:
                        logger.info("  ↳ semantic-merge: removed task_edge %s->%s in %s/%s",
                                    te.get("source"), te.get("target"), aid, said)
                        continue
                    kept.append(te)
                sa["task_edges"] = kept
                if m.get("merged_condition"):
                    kk = _edge_key(m.get("keep", {}))
                    for te in sa["task_edges"]:
                        if _edge_key(te) == kk:
                            te["condition"] = m["merged_condition"]


async def _dedupe_plan_semantic(pipeline: dict, goal: str) -> dict:
    """Pass B: Haiku finds semantically-equivalent sibling branches to merge."""
    edges = [
        {"source": e.get("source"), "target": e.get("target"), "condition": e.get("condition")}
        for e in pipeline.get("edges", [])
    ]
    task_edges = [
        {"agent_id": agent.get("id"), "subagent_id": sa.get("id"),
         "source": te.get("source"), "target": te.get("target"), "condition": te.get("condition")}
        for agent in pipeline.get("agents", [])
        for sa in agent.get("sub_agents", [])
        for te in sa.get("task_edges", [])
    ]
    # Nothing branch-shaped to reason about -- skip the LLM call entirely.
    if len(edges) < 2 and len(task_edges) < 2:
        return pipeline

    last_exc: Exception | None = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            text = await llm_chat(
                system=_VALIDATE_SYSTEM,
                user=_VALIDATE_USER.format(
                    goal=goal[:400],
                    edges=json.dumps(edges, indent=2),
                    task_edges=json.dumps(task_edges, indent=2),
                ),
                max_tokens=512,
                temperature=0,
                tier="fast",
            )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning("validator overloaded (attempt %d/4) -- retrying", attempt + 1)
    else:
        logger.warning("validator LLM unavailable -- skipping semantic pass", exc_info=last_exc)
        return pipeline
    text = _json_object(text)
    try:
        findings = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("validator parse error -- skipping semantic merge  raw=%.120s", text)
        return pipeline

    _apply_merge_findings(pipeline, findings)
    return pipeline


# -- Display-edge synthesis (frontend graph) ----------------------------------
# The runtime gates tasks purely on the typed ``tasks[].condition`` ({symbol,op,value})
# and runs sub-agents in list order -- it never reads explicit edges. The web canvas
# (hospilot-internal/web), however, draws sub-agent and task graphs from
# ``agents[].sub_agent_edges`` and ``agents[].sub_agents[].task_edges`` with STRING
# conditions. These helpers derive those display edges from the typed conditions so the
# canvas renders branches/diamonds instead of falling back to a flat linear chain. The
# typed ``condition`` is left untouched; these fields are additive and purely for display.

def _condition_to_string(cond) -> str | None:
    """Render a typed task condition ({symbol, op, value}) as a short, readable string
    for the canvas (edge labels + per-task pills). Passes a string condition through;
    returns None when there is no condition."""
    if cond is None:
        return None
    if isinstance(cond, str):
        return cond or None
    if not isinstance(cond, dict):
        return None
    symbol = cond.get("symbol") or ""
    field  = symbol.split(".")[-1] or symbol
    op     = cond.get("op") or ""
    value  = cond.get("value")
    if value is None:
        return f"{field} set" if op == "!=" else f"{field} missing"
    if isinstance(value, bool):  # bool before int -- True/False are ints in Python
        positive = value if op == "==" else (not value if op == "!=" else value)
        return field if positive else f"not {field}"
    return f"{field} {op} {value}".strip()


def _build_task_edges(tasks: list[dict]) -> list[dict]:
    """Derive canvas-shaped task_edges from a sub-agent's ordered, typed-conditioned
    tasks. Unconditional tasks form a linear backbone; each conditional task branches
    from the task its condition references (the ``symbol``'s source task), falling back
    to the preceding task when that source is outside this sub-agent."""
    ids = {t.get("id") for t in tasks if t.get("id")}
    edges: list[dict] = []

    backbone = [t for t in tasks if not t.get("condition")]
    for a, b in zip(backbone, backbone[1:]):
        edges.append({"source": a["id"], "target": b["id"]})

    for i, t in enumerate(tasks):
        cond = t.get("condition")
        tid  = t.get("id")
        if not cond or not tid:
            continue
        src = None
        if isinstance(cond, dict) and cond.get("symbol"):
            dep = cond["symbol"].split(".")[0]
            if dep in ids and dep != tid:
                src = dep
        if src is None:
            src = tasks[i - 1].get("id") if i > 0 else None
        if src and src != tid:
            edge = {"source": src, "target": tid}
            cond_str = _condition_to_string(cond)
            if cond_str:
                edge["condition"] = cond_str
            edges.append(edge)
    return edges


# Registered opposite-condition token pairs (mirrors frontend src/lib/layout.ts OPPOSITE_CONDITIONS).
# When two consecutive sub-agents carry tokens from the same pair, _build_sub_agent_edges renders
# them as a YES/NO decision diamond instead of a sequential chain edge.
_OPPOSITE_PAIRS: dict[str, str] = {
    "icu_full":                "icu_not_full",
    "icu_not_full":            "icu_full",
    "er_critical_patients":    "no_er_critical_patients",
    "no_er_critical_patients": "er_critical_patients",
    "beds_available":          "no_beds_available",
    "no_beds_available":       "beds_available",
    "high_acuity":             "low_acuity",
    "low_acuity":              "high_acuity",
    "discharge_ready":         "discharge_not_ready",
    "discharge_not_ready":     "discharge_ready",
    "bed_reserved":            "bed_not_reserved",
    "bed_not_reserved":        "bed_reserved",
    "candidates_found":        "no_candidates_found",
    "no_candidates_found":     "candidates_found",
    "ventilator_needed":       "no_ventilator_needed",
    "no_ventilator_needed":    "ventilator_needed",
    "isolation_needed":        "no_isolation_needed",
    "no_isolation_needed":     "isolation_needed",
    "dirty_beds":              "no_dirty_beds",
    "no_dirty_beds":           "dirty_beds",
}


def _build_sub_agent_edges(sub_agents: list[dict]) -> list[dict]:
    """Build canvas-display edges for the sub-agent graph.

    Unconditional sub-agents form a linear backbone. When two consecutive
    sub-agents carry registered opposite condition tokens (from _OPPOSITE_PAIRS)
    they form a YES/NO decision diamond: both get edges from their common
    predecessor and both converge to their common successor. Non-registered
    single conditions are kept as edge annotations on the linear chain."""
    ids = [sa.get("id") for sa in sub_agents if sa.get("id")]
    by_id = {sa.get("id"): sa for sa in sub_agents if sa.get("id")}
    owner: dict[str, str] = {
        t["id"]: sa.get("id")
        for sa in sub_agents
        for t in (sa.get("tasks") or [])
        if t.get("id")
    }

    if not ids:
        return []

    def _sa_cond(sa_id: str) -> str:
        return (by_id[sa_id].get("condition") or "").strip()

    # Find branch pairs: consecutive SA indices where the two SAs carry registered
    # opposite condition tokens.
    branch_sa_indices: set[int] = set()
    for i in range(len(ids) - 1):
        c_i    = _sa_cond(ids[i])
        c_next = _sa_cond(ids[i + 1])
        if c_i and c_next and _OPPOSITE_PAIRS.get(c_i) == c_next:
            branch_sa_indices.add(i)

    # Edge-level skip set: edge j connects ids[j] → ids[j+1] in the linear chain.
    # Branch handling replaces those edges with explicit diamond edges.
    skip_edge_indices: set[int] = set()
    branch_edges: list[dict] = []

    for bp in sorted(branch_sa_indices):
        # Skip the three linear edges that the diamond replaces:
        #   bp-1: source → ids[bp]         (now a conditional branch edge)
        #   bp:   ids[bp] → ids[bp+1]      (parallel, not sequential)
        #   bp+1: ids[bp+1] → ids[bp+2]    (replaced by convergence edges)
        if bp > 0:
            skip_edge_indices.add(bp - 1)
        skip_edge_indices.add(bp)
        if bp + 2 < len(ids):
            skip_edge_indices.add(bp + 1)

        source    = ids[bp - 1] if bp > 0 else None
        next_node = ids[bp + 2] if bp + 2 < len(ids) else None
        c_yes     = _sa_cond(ids[bp])
        c_no      = _sa_cond(ids[bp + 1])

        if source:
            branch_edges.append({"source": source, "target": ids[bp],     "condition": c_yes})
            branch_edges.append({"source": source, "target": ids[bp + 1], "condition": c_no})
        if next_node:
            branch_edges.append({"source": ids[bp],     "target": next_node})
            branch_edges.append({"source": ids[bp + 1], "target": next_node})

    # Linear backbone: emit edges not consumed by a branch, annotated with any
    # single unambiguous task-level condition gate.
    linear_edges: list[dict] = []
    for i, (a, b) in enumerate(zip(ids, ids[1:])):
        if i in skip_edge_indices:
            continue
        edge: dict = {"source": a, "target": b}
        # Sub-agent-level single condition (not part of a paired branch) goes directly
        # on the edge -- frontend renders it as YES → target, NO → stop node.
        sa_cond = _sa_cond(b)
        if sa_cond:
            edge["condition"] = sa_cond
        else:
            cond_strs: list[str] = []
            for t in (by_id[b].get("tasks") or []):
                cond = t.get("condition")
                if isinstance(cond, dict) and cond.get("symbol"):
                    if owner.get(cond["symbol"].split(".")[0]) == a:
                        cs = _condition_to_string(cond)
                        if cs and cs not in cond_strs:
                            cond_strs.append(cs)
            if len(cond_strs) == 1:
                edge["condition"] = cond_strs[0]
        linear_edges.append(edge)

    return branch_edges + linear_edges


def _synthesize_display_edges(pipeline: dict) -> None:
    """Attach canvas-display edges to the plan IN PLACE. Adds
    ``agents[].sub_agents[].task_edges`` (string conditions) and ``agents[].sub_agent_edges``,
    derived from the typed task conditions and sub-agent order. Additive only -- the typed
    ``tasks[].condition`` the materializer/runtime read is left untouched."""
    for agent in pipeline.get("agents", []):
        sub_agents = agent.get("sub_agents") or []
        for sa in sub_agents:
            sa["task_edges"] = _build_task_edges(sa.get("tasks") or [])
        agent["sub_agent_edges"] = _build_sub_agent_edges(sub_agents)


async def validate_and_dedupe_plan(pipeline: dict, goal: str = "", session_id: str = "") -> dict:
    """Stage 3b: remove/merge redundant steps and branches before approval.

    Deterministic structural dedupe followed by a Haiku semantic-merge pass. Never
    raises -- on any error returns the best (most-deduped) plan produced so far. Runs
    the existing structural safety nets afterwards and rolls back if an agent or
    sub-agent was unexpectedly dropped.
    """
    agents_before = len(pipeline.get("agents", []))
    sas_before = sum(len(a.get("sub_agents", [])) for a in pipeline.get("agents", []))
    try:
        _dedupe_plan_structural(pipeline)
    except Exception:  # noqa: BLE001
        logger.warning("structural dedupe failed -- skipping", exc_info=True)

    safe = pipeline  # the structurally-deduped plan we can always fall back to
    try:
        pipeline = await _dedupe_plan_semantic(pipeline, goal)
    except Exception:  # noqa: BLE001
        logger.warning("semantic dedupe failed -- using structural result", exc_info=True)
        pipeline = safe

    # Safety nets: keep task references catalog-valid and drop dangling edges.
    try:
        _avail, _sub, _ind, sa_task_ids = await _fetch_registry()
        _sanitize_sub_agent_tasks(pipeline.get("agents", []), sa_task_ids=sa_task_ids)
        known = {a["id"] for a in pipeline.get("agents", [])}
        pipeline["edges"] = [
            e for e in pipeline.get("edges", [])
            if e.get("source") in known and e.get("target") in known
        ]
    except Exception:  # noqa: BLE001
        logger.warning("post-dedupe safety net failed", exc_info=True)

    # Roll back if cleanup unexpectedly removed an agent or sub-agent.
    agents_after = len(pipeline.get("agents", []))
    sas_after = sum(len(a.get("sub_agents", [])) for a in pipeline.get("agents", []))
    if agents_after < agents_before or sas_after < sas_before:
        logger.warning("dedupe dropped structure (agents %d->%d, sub-agents %d->%d) -- rolling back",
                       agents_before, agents_after, sas_before, sas_after)
        await _inject_patient_verification(safe, session_id=session_id, goal=goal)
        _synthesize_display_edges(safe)
        return safe

    # Lead with patient_verification_agent when any selected task requires identity
    # (stage 3b: the selected tasks now exist). Then derive canvas-display edges
    # (sub_agent_edges + per-sub-agent task_edges) -- last, so it sees the PV edges too.
    await _inject_patient_verification(pipeline, session_id=session_id, goal=goal)
    _synthesize_display_edges(pipeline)
    logger.info("<- [stage3b] plan validated  agents=%d  edges=%d",
                len(pipeline.get("agents", [])), len(pipeline.get("edges", [])))
    return pipeline


# -- Plan critic (automated quality gate) -------------------------------------

_CRITIC_SYSTEM = _load_prompt("critic.txt")

_CRITIC_USER = """Goal: {goal}

Proposed plan (agents and edges):
{plan}

Return ONLY this JSON:
{{"verdict": "pass" | "needs_revision",
  "score": <int 0-10>,
  "findings": ["short principle notes"],
  "revision_instruction": "<one concrete instruction, or empty string if pass>"}}"""


async def critique_pipeline(pipeline: dict, goal: str) -> dict:
    """LLM critic over the agent graph. FAIL-OPEN: any error returns verdict 'pass' so the
    critic can never block approval."""
    compact = {
        "understood_goal": pipeline.get("understood_goal", ""),
        "agents": [
            {"id": a["id"], "role": a.get("role", ""), "task_type": a.get("task_type")}
            for a in pipeline.get("agents", [])
        ],
        "edges": [
            {"source": e.get("source"), "target": e.get("target"), "condition": e.get("condition")}
            for e in pipeline.get("edges", [])
        ],
    }
    try:
        text = await llm_chat(
            system=_CRITIC_SYSTEM,
            user=_CRITIC_USER.format(goal=goal, plan=json.dumps(compact, indent=2)),
            max_tokens=1024,
            temperature=0,
            tier="quality",
        )
        # Carve out the JSON object even if the model wraps it in prose or fences.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            logger.warning("plan critic returned no JSON object -- defaulting to pass (fail-open)")
            return {"verdict": "pass", "score": 10, "findings": [], "revision_instruction": ""}
        report = json.loads(text[start:end + 1])
        logger.info("plan critic  verdict=%s  score=%s", report.get("verdict"), report.get("score"))
        return report
    except Exception:
        logger.exception("plan critic failed -- defaulting to pass (fail-open)")
        return {"verdict": "pass", "score": 10, "findings": [], "revision_instruction": ""}
