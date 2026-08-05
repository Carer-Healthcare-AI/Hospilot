"""Rewrite import paths after monorepo restructure."""
import os, re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Order matters: longest/most-specific patterns first to avoid partial matches.
RENAMES = [
    # ── Lab activities ────────────────────────────────────────────────────────
    ("temporal.activities.lab_analyzer_routing_activities",     "agents.lab.analyzer_routing"),
    ("temporal.activities.lab_analyzer_utilization_activities", "agents.lab.analyzer_utilization"),
    ("temporal.activities.lab_capacity_prediction_activities",  "agents.lab.capacity_prediction"),
    ("temporal.activities.lab_critical_result_activities",      "agents.lab.critical_result"),
    ("temporal.activities.lab_quality_control_activities",      "agents.lab.quality_control"),
    ("temporal.activities.lab_sample_prioritization_activities","agents.lab.sample_prioritization"),
    ("temporal.activities.lab_sample_tracking_activities",      "agents.lab.sample_tracking"),
    ("temporal.activities.lab_tat_activities",                  "agents.lab.tat"),
    ("temporal.activities.lab_test_recommendation_activities",  "agents.lab.test_recommendation"),
    ("temporal.activities.lab_test_validation_activities",      "agents.lab.test_validation"),
    ("temporal.activities.lab_activities",                      "agents.lab.activities"),
    # ── Pharmacy activities ───────────────────────────────────────────────────
    ("temporal.activities.pharmacy_capacity_activities",                "agents.pharmacy.capacity"),
    ("temporal.activities.pharmacy_clinical_interaction_activities",    "agents.pharmacy.clinical_interaction"),
    ("temporal.activities.pharmacy_controlled_drug_activities",         "agents.pharmacy.controlled_drug"),
    ("temporal.activities.pharmacy_dispensing_validation_activities",   "agents.pharmacy.dispensing_validation"),
    ("temporal.activities.pharmacy_drug_availability_activities",       "agents.pharmacy.drug_availability"),
    ("temporal.activities.pharmacy_fulfillment_activities",             "agents.pharmacy.fulfillment"),
    ("temporal.activities.pharmacy_prescription_validation_activities", "agents.pharmacy.prescription_validation"),
    ("temporal.activities.pharmacy_prioritization_activities",          "agents.pharmacy.prioritization"),
    ("temporal.activities.pharmacy_queue_activities",                   "agents.pharmacy.queue"),
    ("temporal.activities.pharmacy_substitution_activities",            "agents.pharmacy.substitution"),
    ("temporal.activities.pharmacy_activities",                         "agents.pharmacy.activities"),
    # ── Other domain activities ───────────────────────────────────────────────
    ("temporal.activities.ambulance_activities",          "agents.ambulance.activities"),
    ("temporal.activities.appointment_activities",        "agents.appointment.activities"),
    ("temporal.activities.bed_agent_activities",          "agents.bed.agent_activities"),
    ("temporal.activities.bed_prediction_activities",     "agents.bed.prediction_activities"),
    ("temporal.activities.bed_activities",                "agents.bed.activities"),
    ("temporal.activities.billing_activities",            "agents.billing.activities"),
    ("temporal.activities.discharge_activities",          "agents.discharge.activities"),
    ("temporal.activities.er_activities",                 "agents.er.activities"),
    ("temporal.activities.housekeeping_activities",       "agents.housekeeping.activities"),
    ("temporal.activities.icu_activities",                "agents.icu.activities"),
    ("temporal.activities.ot_activities",                 "agents.ot.activities"),
    ("temporal.activities.revenue_activities",            "agents.revenue.activities"),
    ("temporal.activities.staff_activities",              "agents.staff.activities"),
    # ── Shared activities ─────────────────────────────────────────────────────
    ("temporal.activities.approval_escalation_activities", "workflows.approval_escalation_activities"),
    ("temporal.activities.builtin_tasks",                  "agents._shared.builtin_tasks"),
    ("temporal.activities.condition_activities",           "agents._shared.condition_activities"),
    ("temporal.activities.generated_activities",           "agents._shared.generated_activities"),
    ("temporal.activities.generic_activities",             "agents._shared.generic_activities"),
    ("temporal.activities.prefetch_activities",            "agents._shared.prefetch_activities"),
    # ── Workflow-level services ───────────────────────────────────────────────
    ("services.planner",          "workflows.planner"),
    ("services.materializer",     "workflows.materializer"),
    ("services.strategies",       "workflows.strategies"),
    ("services.unified_executor", "workflows.unified_executor"),
    ("services.task_codegen",     "workflows.task_codegen"),
    ("services.task_writer",      "workflows.task_writer"),
    ("services.dynamic_task",     "workflows.dynamic_task"),
    # ── Agent services ────────────────────────────────────────────────────────
    ("services.ambulance",     "agents.ambulance.service"),
    ("services.bed_prediction","agents.bed.service"),
    ("services.discharge",     "agents.discharge.service"),
    ("services.er_triage",     "agents.er.service"),
    ("services.icu",           "agents.icu.service"),
    ("services.ot",            "agents.ot.service"),
    ("services.staff",         "agents.staff.service"),
    # ── Shared agent utilities ────────────────────────────────────────────────
    ("services.agent_schemas", "agents._shared.agent_schemas"),
    ("services.fetch_tools",   "agents._shared.fetch_tools"),
    ("services.guardrail",     "agents._shared.guardrail"),
    ("services.manifest",      "agents._shared.manifest"),
    ("services.ranking",       "agents._shared.ranking"),
    # ── Graph / temporal top-level ────────────────────────────────────────────
    ("from graph.",             "from workflows.graph."),
    ("import graph.",           "import workflows.graph."),
    ("from temporal.workflow.", "from workflows.temporal.workflow."),
    ("from temporal.worker.",   "from workflows.temporal.worker."),
    ("from temporal.client",    "from workflows.temporal.client"),
    ("import temporal.client",  "import workflows.temporal.client"),
    # ── Poller ────────────────────────────────────────────────────────────────
    ("from poller.",   "from kafka.poller."),
    ("import poller.", "import kafka.poller."),
]

IMPORT_RE = re.compile(r"^\s*(from|import)\s")

def rewrite_line(line: str) -> str:
    if not IMPORT_RE.match(line):
        return line
    for old, new in RENAMES:
        if old in line:
            line = line.replace(old, new, 1)
            break  # apply at most one rename per line
    return line

def rewrite_file(path: str) -> bool:
    with open(path, encoding="utf-8") as f:
        original = f.read()
    lines = original.splitlines(keepends=True)
    new_lines = [rewrite_line(l) for l in lines]
    rewritten = "".join(new_lines)
    if rewritten == original:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(rewritten)
    return True

changed, skipped = [], []
for root, dirs, files in os.walk(BASE):
    # skip this script itself and __pycache__
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for fname in files:
        if not fname.endswith(".py"):
            continue
        path = os.path.join(root, fname)
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        if rewrite_file(path):
            changed.append(os.path.relpath(path, BASE))
        else:
            skipped.append(os.path.relpath(path, BASE))

print(f"\nChanged  : {len(changed)}")
print(f"Unchanged: {len(skipped)}")
if changed:
    print("\nModified files:")
    for f in sorted(changed):
        print(f"  {f}")
