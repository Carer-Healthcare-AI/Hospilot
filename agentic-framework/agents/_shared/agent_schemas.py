"""
Static schema context for task codegen.

Maps each agent -> its Hasura tables -> real column names extracted from hasura.py queries.
Passed to Claude during code generation so it uses actual field names, not invented ones.
"""

# fmt: off
AGENT_SCHEMAS: dict[str, dict[str, list[str]]] = {

    "er_agent": {
        "hospilot_visits": [
            "id",                  # uuid -- visit PK
            "patient_token",       # uuid -- patient identifier
            "department_id",       # uuid
            "arrived_at",          # timestamptz -- use this to calculate wait time / LOS
            "status",              # String: "waiting" | "in_treatment" | "discharged" | "admitted"
            "chief_complaint",     # String
            "triage_score",        # Int: 1=most critical, 5=least critical (CTAS scale)
        ],
    },

    "icu_agent": {
        "hospilot_ipd_admissions": [
            "id",                        # uuid
            "patient_token",             # uuid
            "bed_id",                    # uuid
            "department_id",             # uuid
            "admitted_at",               # timestamptz -- use this to calculate ICU LOS
            "expected_discharge_at",     # timestamptz
            "status",                    # String: "admitted" | "discharged"
            "discharge_ready",           # Boolean
            "discharge_blocked_reason",  # String | null
            "transfer_pending",          # Boolean -- true if transfer already in progress
            # nested relation: bed { ward, room_type, ventilation, features, status }
        ],
        "hospilot_beds": [
            "id",            # uuid
            "ward",          # String -- contains "ICU" for ICU wards
            "bed_number",    # String
            "room_type",     # String
            "status",        # String: "Available" | "Occupied" | "Dirty" | "Cleaning" | "reserved"
            "is_active",     # Boolean
            "ventilation",   # String: "full_ventilator" | "bipap" | "none"
            "features",      # jsonb
        ],
        "hospilot_vitals": [
            "id",               # uuid
            "patient_token",    # uuid
            "admission_id",     # uuid
            "recorded_at",      # timestamptz
            "temperature",      # numeric
            "pulse",            # Int
            "bp_systolic",      # Int
            "bp_diastolic",     # Int
            "spo2",             # numeric
            "respiratory_rate", # Int
            "gcs",              # Int -- Glasgow Coma Scale
            "is_critical",      # Boolean
        ],
    },

    "bed_agent": {
        "hospilot_beds": [
            "id",            # uuid
            "ward",          # String
            "bed_number",    # String
            "room_type",     # String
            "status",        # String: "Available" | "Occupied" | "Dirty" | "Cleaning" | "reserved"
            "is_active",     # Boolean
            "branch_id",     # uuid
            "ventilation",   # String
            "room_sharing",  # String
            "proximity",     # String
            "floor",         # String
            "wing",          # String
            "natural_light", # String
            "noise_level",   # String
            "features",      # jsonb
        ],
        "hospilot_ipd_admissions": [
            "id", "patient_token", "bed_id", "admitted_at",
            "expected_discharge_at", "status", "discharge_ready", "transfer_pending",
        ],
    },

    "discharge_agent": {
        "hospilot_ipd_admissions": [
            "id",                        # uuid
            "patient_token",             # uuid
            "bed_id",                    # uuid
            "department_id",             # uuid
            "admitted_at",               # timestamptz
            "expected_discharge_at",     # timestamptz -- compare to now() for overdue
            "status",                    # String
            "discharge_ready",           # Boolean
            "discharge_blocked_reason",  # String | null
            "transfer_pending",          # Boolean
        ],
        "hospilot_discharge_summaries": [
            "id",                  # uuid
            "admission_id",        # uuid -- FK to hospilot_ipd_admissions.id
            "summary_text",        # String
            "ai_generated_note",   # String | null
            "created_at",          # timestamptz
        ],
        "hospilot_nursing_tasks": [
            "id",           # uuid
            "admission_id", # uuid
            "task",         # String -- task description
            "completed",    # Boolean
            "due_at",       # timestamptz
            "assigned_to",  # String
        ],
    },

    "staff_agent": {
        "hospilot_ipd_admissions": [
            "id", "patient_token", "bed_id", "admitted_at",
            "status",
            # nested: bed { ward, room_type }
        ],
        "hospilot_nursing_tasks": [
            "id", "admission_id", "task", "completed", "due_at", "assigned_to",
        ],
    },

    "pharmacy_agent": {
        "hospilot_ipd_admissions": [
            "id", "patient_token", "admitted_at", "expected_discharge_at",
            "discharge_ready", "status",
        ],
        "hospilot_discharge_summaries": [
            "id", "admission_id", "summary_text", "ai_generated_note", "created_at",
        ],
    },

    "ot_agent": {
        "ot_surgeries": [
            # CarerOS table, nested inside ipd_admissions
            "id",            # uuid
            "status",        # String: "Scheduled" | "In Progress" | "Completed" | "Cancelled"
            "created_at",    # timestamptz -- surgery scheduled/created time
            "admission_id",  # uuid (added by the query join)
            "ward",          # String (from parent admission)
            "patient_id",    # uuid (from parent admission)
        ],
        "hospilot_beds": [
            "id", "ward", "bed_number", "room_type", "status", "ventilation",
        ],
    },

    "housekeeping_agent": {
        "hospilot_beds": [
            "id", "ward", "bed_number", "room_type",
            "status",  # "Dirty" | "Cleaning" | "Available" | "Occupied"
            "is_active",
        ],
        "hospilot_ipd_admissions": [
            "id", "bed_id", "patient_token", "admitted_at", "discharge_ready", "status",
        ],
    },

    "bed_prediction_agent": {
        "hospilot_beds": [
            "id", "ward", "bed_number", "room_type", "status", "is_active",
            "ventilation", "features",
        ],
        "hospilot_ipd_admissions": [
            "id", "patient_token", "bed_id", "admitted_at",
            "expected_discharge_at", "status", "discharge_ready",
        ],
    },

    "revenue_agent": {
        "hospilot_invoices": [
            "id",              # uuid
            "invoice_number",  # String
            "patient_token",   # uuid
            "admission_id",    # uuid | null
            "visit_id",        # uuid | null
            "invoice_type",    # String
            "invoice_date",    # date
            "due_date",        # date
            "grand_total",     # numeric
            "paid_amount",     # numeric
            "balance",         # numeric -- amount still owed
            "status",          # String
            "payment_status",  # String: "Unpaid" | "Partial" | "Paid"
        ],
        "hospilot_daily_collections": [
            "id", "collection_date", "total_collection",
            "cash_total", "upi_total", "card_total", "bank_transfer_total",
            "invoice_count", "payment_count", "is_reconciled", "variance",
        ],
    },

    "billing_agent": {
        "claims": [
            # CarerOS table
            "id", "patient_id", "visit_id", "tpa_id", "tpa_name",
            "claim_amount", "status", "created_at",
            "submitted_date", "approved_amount", "denial_reason",
            "claim_number", "payer_type", "risk_level", "risk_score",
            "stage", "compliance_status", "diagnosis_code",
        ],
        "hospilot_invoices": [
            "id", "invoice_number", "patient_token", "visit_id", "grand_total",
            "paid_amount", "balance", "payment_status", "invoice_date", "due_date",
        ],
    },

    "lab_agent": {
        "hospilot_lab_orders": [
            "id",           # uuid
            "visit_id",     # uuid
            "patient_token", # uuid
            "ordered_by",   # String
            "status",       # String: "Pending" | "In Progress" | "Completed"
            "priority",     # String
            "ordered_at",   # timestamptz -- use this + completed_at for TAT
            "completed_at", # timestamptz | null
        ],
        "hospilot_lab_results": [
            "id", "order_id", "patient_token", "test_name", "test_code",
            "result_value", "flag", "reference_range", "unit", "reported_at",
        ],
    },

}
# fmt: on


def get_schema_context(agent_id: str) -> str:
    """Return formatted schema string for the codegen prompt."""
    base = agent_id.split(":")[0]
    tables = AGENT_SCHEMAS.get(base, {})
    if not tables:
        return "(schema not available for this agent)"
    lines = ["Real table schemas -- use ONLY these column names, do not invent others:"]
    for table, cols in tables.items():
        # strip inline comments for cleaner output, keep just the name
        clean_cols = []
        for col in cols:
            name = col.split("#")[0].strip()
            if name:
                clean_cols.append(name)
        lines.append(f"  {table}: {', '.join(clean_cols)}")
    lines.append("")
    lines.append("Key notes:")
    lines.append("  - LOS / wait time: calculate from arrived_at or admitted_at (timestamptz) to now(), NOT from a los_minutes field (it does not exist)")
    lines.append("  - triage_score: 1=most critical, 5=least critical (CTAS scale)")
    lines.append("  - bed status values: 'Available', 'Occupied', 'Dirty', 'Cleaning', 'reserved'")
    lines.append("  - visit status values: 'waiting', 'in_treatment', 'discharged', 'admitted'")
    return "\n".join(lines)
