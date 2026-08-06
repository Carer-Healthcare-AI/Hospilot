"""
Dynamic task fetch tool registry.

Maps agent_id -> {tool_name: async_callable} for use in the tool-use loop
inside execute_dynamic_task. Each callable wraps an existing HasuraClient method.

Tool schemas (Anthropic tool_use format) live in manifest.py alongside the data
source declarations; implementations live here to keep manifest.py import-free.
"""

from db.hasura import hasura

AGENT_FETCH_TOOLS: dict[str, dict] = {

    "er_agent": {
        "fetch_er_visits":       lambda: hasura.get_active_er_visits(),
        "fetch_beds":            lambda: hasura.get_enriched_beds(),
        "fetch_long_wait_visits": lambda: hasura.get_long_wait_er_visits(),
    },

    "icu_agent": {
        "fetch_icu_admissions":    lambda: hasura.get_icu_admissions(),
        "fetch_available_icu_beds": lambda: hasura.get_available_icu_beds(),
        "fetch_dirty_icu_beds":    lambda: hasura.get_dirty_icu_beds(),
    },

    "bed_agent": {
        "fetch_beds":         lambda: hasura.get_enriched_beds(),
        "fetch_admissions":   lambda: hasura.get_discharge_eligible_admissions(),
        "fetch_beds_summary": lambda: hasura.get_beds_summary(),
    },

    "discharge_agent": {
        "fetch_discharge_eligible":  lambda: hasura.get_discharge_eligible_admissions(),
        "fetch_discharge_summaries": lambda: hasura.carerOS_get_discharge_summaries(),
    },

    "staff_agent": {
        "fetch_admissions_with_wards": lambda: hasura.get_admissions_with_wards(),
        "fetch_nursing_tasks":         lambda: hasura.get_all_incomplete_tasks(),
    },

    "pharmacy_agent": {
        "fetch_discharge_with_summaries": lambda: hasura.get_discharge_ready_with_summaries(),
    },

    "billing_agent": {
        "fetch_outstanding_invoices": lambda: hasura.get_outstanding_invoices(),
        "fetch_claims":               lambda: hasura.carerOS_get_claims(),
    },

    "revenue_agent": {
        "fetch_outstanding_invoices": lambda: hasura.get_outstanding_invoices(),
        "fetch_daily_collections":    lambda: hasura.carerOS_get_daily_collections(),
        "fetch_todays_collections":   lambda: hasura.get_todays_collections(),
    },

    "ot_agent": {
        "fetch_ot_surgeries": lambda: hasura.carerOS_get_ot_surgeries(),
        "fetch_postop_beds":  lambda: hasura.get_available_postop_beds(),
    },

    "housekeeping_agent": {
        "fetch_dirty_beds":              lambda: hasura.get_dirty_beds(),
        "fetch_recently_discharged_beds": lambda: hasura.get_recently_discharged_beds(),
    },

    "appointment_agent": {
        "fetch_appointments":    lambda: hasura.appt_list_appointments(),
        "fetch_available_slots": lambda: hasura.appt_available_slots(),
    },
}


def get_fetch_tools(agent_id: str) -> dict:
    """Return fetch implementations for agent_id, stripping :N suffix."""
    base = agent_id.split(":")[0]
    return AGENT_FETCH_TOOLS.get(base) or AGENT_FETCH_TOOLS.get(agent_id) or {}
