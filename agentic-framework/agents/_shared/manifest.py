"""
AGENT_DATA_MANIFEST -- declares what data each agent can access at runtime.

Used by two guardrail checkpoints:
  1. Creation-time (validate_new_task): before writing a new dynamic task to
     task_registry, Haiku checks if the task's data requirements are satisfiable
     from the sources listed here.
  2. Execution-time (execute_dynamic_task): before running an unrecognised task
     ID via Claude, the worker verifies live session context keys cover the
     task's needs.

Add a new entry here whenever a new agent is wired up, or when an existing
agent gains access to a new table / Redis key.
"""

from dataclasses import dataclass, field


@dataclass
class AgentDataSources:
    redis_keys: list[str]      # Redis key patterns available to this agent
    hasura_tables: list[str]   # PostgreSQL tables this agent can read
    context_fields: list[str]  # Session-context keys produced by prior tasks
    description: str           # One-line description used in guardrail prompts
    tool_schemas: list[dict] = field(default_factory=list)  # Anthropic tool_use schemas


AGENT_DATA_MANIFEST: dict[str, AgentDataSources] = {

    "bed_agent": AgentDataSources(
        redis_keys=[
            "bed:{id}", "bed:ids",
            "admission:{id}", "admission:ids",
            "dept:{id}", "dept:ids",
        ],
        hasura_tables=[
            "hospilot_beds", "hospilot_admissions", "hospilot_departments",
        ],
        context_fields=[
            "_goal", "_task_type",
            "ta_query_beds", "ta_check_dirty_icu_beds",
            "ta_rank_bed", "ta_reserve_bed",
            "candidate_count", "dirty_beds", "reserved_bed_id",
        ],
        description="Bed availability, status, ranking, reservations, dirty-bed recovery",
        tool_schemas=[
            {"name": "fetch_beds",         "description": "Get all active beds with status, ward, room type, ventilation features", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_admissions",   "description": "Get current admitted patients with bed and expected discharge info",      "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_beds_summary", "description": "Get aggregate bed counts: total, available, ICU vs ward breakdown",       "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "bed_prediction_agent": AgentDataSources(
        redis_keys=[
            "bed:{id}", "bed:ids",
            "admission:{id}", "admission:ids",
        ],
        hasura_tables=["hospilot_beds", "hospilot_admissions"],
        context_fields=[
            "_goal", "ta_bed_prediction", "predicted_demand",
            "occupancy_rate", "predicted_shortage",
        ],
        description="Bed demand forecasting, occupancy trend analysis",
        tool_schemas=[
            {"name": "fetch_beds_summary",     "description": "Get total/available/ICU bed counts and occupancy percentages",           "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_discharge_horizon", "description": "Get count of patients expected to discharge within the next 6 hours",    "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_er_pressure",       "description": "Get ER visit counts by CTAS tier -- proxy for upcoming admission demand", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "icu_agent": AgentDataSources(
        redis_keys=[
            "bed:{id}", "bed:ids",
            "vital:{patient_token}",
            "admission:{id}", "admission:ids",
        ],
        hasura_tables=[
            "hospilot_beds", "hospilot_patients", "hospilot_admissions",
        ],
        context_fields=[
            "_goal",
            "ta_icu_census", "ta_ventilator_count", "ta_step_down",
            "icu_census", "ventilator_count", "step_down_candidates",
            "available_icu_beds",
        ],
        description="ICU census, ventilator tracking, step-down candidate identification",
        tool_schemas=[
            {"name": "fetch_icu_admissions",     "description": "Get patients currently admitted to ICU beds with bed details and discharge readiness", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_available_icu_beds",  "description": "Get available (unoccupied) ICU beds with ventilation and feature info",               "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_dirty_icu_beds",      "description": "Get dirty ICU beds that could be fast-tracked to available",                          "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "er_agent": AgentDataSources(
        redis_keys=[
            "visit:{id}", "visit:ids",
            "vital:{patient_token}",
            "bed:{id}", "bed:ids",
        ],
        hasura_tables=["visits", "hospilot_beds", "hospilot_patients"],
        context_fields=[
            "_goal",
            "ta_er_triage", "ta_admission_router", "ta_fasttrack_router",
            "urgency_scores", "boarding_patients", "triage_queue",
            "fast_track_eligible",
        ],
        description="ER queue management, triage scoring, admission and fast-track routing",
        tool_schemas=[
            {"name": "fetch_er_visits",        "description": "Get active ER visits with patient token, triage score, chief complaint, wait status", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_beds",             "description": "Get all active beds with status, ward, room type, ventilation",                       "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_long_wait_visits", "description": "Get ER patients who have been waiting more than 2 hours",                             "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "staff_agent": AgentDataSources(
        redis_keys=[
            "dept:{id}", "dept:ids",
            "admission:{id}", "admission:ids",
        ],
        hasura_tables=["departments", "hospilot_staff", "hospilot_shifts"],
        context_fields=[
            "_goal",
            "ta_ratio_monitor", "ta_float_pool",
            "nurse_ratios", "float_available", "understaffed_wards",
        ],
        description="Nurse-patient ratios, float pool availability, shift staffing levels",
        tool_schemas=[
            {"name": "fetch_admissions_with_wards", "description": "Get all admitted patients with ward and bed info -- for workload analysis", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_nursing_tasks",          "description": "Get all incomplete nursing tasks with assignment and due time",            "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "discharge_agent": AgentDataSources(
        redis_keys=[
            "admission:{id}", "admission:ids",
            "bed:{id}", "bed:ids",
            "dept:{id}",
        ],
        hasura_tables=[
            "hospilot_admissions", "hospilot_beds", "discharge_summaries",
        ],
        context_fields=[
            "_goal",
            "ta_discharge_ready", "ta_discharge_barriers",
            "discharge_candidates", "barriers", "barrier_types",
        ],
        description="Discharge readiness, barrier identification, bed turnover coordination",
        tool_schemas=[
            {"name": "fetch_discharge_eligible",  "description": "Get admissions near or past expected discharge date with barrier info", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_discharge_summaries", "description": "Get recent discharge summaries from CarerOS",                           "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "pharmacy_agent": AgentDataSources(
        redis_keys=["dept:{id}", "dept:ids"],
        hasura_tables=["hospilot_drug_inventory", "hospilot_drug_orders"],
        context_fields=[
            "_goal",
            "ta_stock_monitor",
            "critical_shortages", "drug_levels", "expiring_drugs",
        ],
        description="Drug inventory levels, critical shortage alerts, order tracking",
        tool_schemas=[
            {"name": "fetch_discharge_with_summaries", "description": "Get discharge-ready patients with their discharge summary and medication notes", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "ot_agent": AgentDataSources(
        redis_keys=[
            "bed:{id}", "bed:ids",
            "admission:{id}", "admission:ids",
        ],
        hasura_tables=[
            "hospilot_ot_schedule", "hospilot_beds", "hospilot_admissions",
        ],
        context_fields=[
            "_goal",
            "ta_ot_census", "ta_ot_capacity_analyser",
            "surgical_list", "post_op_beds", "conflicts", "recommendations",
        ],
        description="Surgical schedule, OT capacity, post-operative bed availability",
        tool_schemas=[
            {"name": "fetch_ot_surgeries", "description": "Get scheduled OT cases with admission, ward, and surgery status", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_postop_beds",  "description": "Get available non-ICU beds suitable for post-operative patients", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "revenue_agent": AgentDataSources(
        redis_keys=[
            "invoice:{id}", "invoice:ids",
            "claim:{id}", "claim:ids",
            "collections:{date}",
        ],
        hasura_tables=[
            "hospilot_invoices", "hospilot_payments",
            "hospilot_daily_collections", "claims", "discharge_summaries",
            "departments",
        ],
        context_fields=[
            "_goal",
            "ta_identify_revenue_leakage", "ta_optimize_package_utilization",
            "ta_predict_denial_risk_rev", "ta_escalation_recommendations_rev",
            "leakage_amount", "high_risk_claims", "high_risk_count",
            "denial_patterns",
        ],
        description="Billing-gap & leakage review, package/dept profitability, resource utilisation, and insurance denial-risk prediction & prevention",
        tool_schemas=[
            {"name": "fetch_outstanding_invoices", "description": "Get invoices with unpaid or partial balance -- the revenue gap",              "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_daily_collections",    "description": "Get last 30 days of daily collection summaries with cash/UPI/card breakdown", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_todays_collections",   "description": "Get today's collection totals and reconciliation status",                     "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "billing_agent": AgentDataSources(
        redis_keys=[
            "invoice:{id}", "invoice:ids",
            "claim:{id}", "claim:ids",
            "claim:lines:{id}", "claim:history:{id}",
            "collections:{date}",
        ],
        hasura_tables=[
            "hospilot_invoices", "hospilot_payments",
            "claims", "hospilot_audit_log",
        ],
        context_fields=[
            "_goal",
            "ta_detect_claim_discrepancies", "ta_validate_insurance_eligibility",
            "ta_check_billing_compliance", "ta_track_pending_payments",
            "ta_get_patient_invoices", "ta_create_billing_request",
            "overdue_count", "no_tpa_pending_count",
            "unverified_amount", "billing_requests",
        ],
        description="Structural claim validation, collections & payment recovery, patient invoice/claim lookup, and bill generation",
        tool_schemas=[
            {"name": "fetch_outstanding_invoices", "description": "Get invoices with unpaid or partial balance", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_claims",               "description": "Get insurance claims with risk score, denial reason, status, TPA details", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "housekeeping_agent": AgentDataSources(
        redis_keys=["bed:{id}", "bed:ids"],
        hasura_tables=["hospilot_beds", "hospilot_housekeeping_tasks"],
        context_fields=[
            "_goal",
            "ta_clean_vacated_beds",
            "dirty_beds", "cleaning_tasks", "beds_ready",
        ],
        description="Dirty-bed recovery, cleaning task dispatch, bed readiness tracking",
        tool_schemas=[
            {"name": "fetch_dirty_beds",               "description": "Get all dirty beds of any type across all wards",                       "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_recently_discharged_beds", "description": "Get beds from recently discharge-ready admissions needing housekeeping", "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),

    "appointment_agent": AgentDataSources(
        redis_keys=[],
        hasura_tables=["hospilot_appointments", "hospilot_doctor_slots", "hospilot_visits"],
        context_fields=[
            "_goal",
            "ta_appt_find_available_slots", "ta_appt_get_due_reminders", "ta_appt_predict_noshow",
            "available_slot_count", "due_count", "high_risk_count", "chronic_count",
        ],
        description="OPD appointment scheduling, reminders, no-show prediction and prevention",
        tool_schemas=[
            {"name": "fetch_appointments",    "description": "Get OPD appointments with patient contact, provider specialty, status, time", "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "fetch_available_slots", "description": "Get bookable doctor slots (Available, not full) with provider specialty",       "input_schema": {"type": "object", "properties": {}, "required": []}},
        ],
    ),
}


def get_manifest(agent_id: str) -> AgentDataSources | None:
    """Return manifest for agent_id, stripping :N suffix (e.g. bed_agent:1 -> bed_agent)."""
    base = agent_id.split(":")[0]
    return AGENT_DATA_MANIFEST.get(base) or AGENT_DATA_MANIFEST.get(agent_id)
