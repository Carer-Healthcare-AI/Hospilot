"""The flow catalog: six end-to-end pipelines, defined once and shared.

Each entry is a real pipeline dict of exactly the shape `build_session_graph`
consumes — `{"agents": [...], "edges": [...]}` — so a flow test drives the same
graph builder the product uses, not a test-only imitation.

Coverage rule, enforced by `test_flow_coverage.py`: the five themed flows must
between them touch every plannable agent in the registry, and FLOW_ALL must use
all of them in one pipeline. If an agent is added to the registry and to no flow,
that test fails — which is the point. Keeping the catalog here (rather than
inline in the test files) is what makes that check possible.

Levels matter: `edges` drive topological levelling, and a whole level completes
in one LangGraph superstep before the next begins. The flows below are shaped so
each has at least two levels, because a single-level pipeline would never
exercise the BSP barrier that most cross-agent bugs hide behind.
"""

# Every plannable agent in the registry (workflows/planner.py SUB_AGENTS).
# patient_verification_agent is deliberately excluded from the themed flows: it
# is injected by the planner for patient-specific goals and parks on a HITL
# interrupt, so it belongs to the interrupt tests rather than a straight-through
# flow. FLOW_ALL covers it explicitly.
ALL_PLANNABLE = [
    "bed_agent", "icu_agent", "er_agent", "staff_agent", "ambulance_agent",
    "discharge_agent", "pharmacy_agent", "lab_agent", "ot_agent",
    "revenue_agent", "billing_agent",
]

# Registry agents that no flow covers yet, and why. Adding an agent to the
# registry should NOT break CI before anyone has had a chance to write a flow for
# it — put it here in the same PR, then remove the entry when the flow lands.
#
# The list is deliberately visible rather than a silent exemption: it is the
# to-do list of missing flow coverage, and it should trend toward empty.
PENDING_FLOW_COVERAGE = {
    "patient_verification_agent": (
        "planner-injected for patient-specific goals and parks on a HITL "
        "interrupt, so it has no place in a straight-through flow. Belongs to "
        "the interrupt tests instead — not expected to gain a flow."
    ),
}


def _agent(aid: str, label: str = "", **extra) -> dict:
    a = {"id": aid, "label": label or aid.replace("_", " ").title()}
    a.update(extra)
    return a


def _edge(src: str, tgt: str, condition: str = "") -> dict:
    e = {"source": src, "target": tgt}
    if condition:
        e["condition"] = condition
    return e


# ── 1. ER admission ──────────────────────────────────────────────────────────
# An ambulance arrival triaged in the ER, then bedded with staff assigned.
# Shape: ambulance -> er -> (bed, staff) — a fan-out at the last level.
FLOW_ER_ADMISSION = {
    "name": "er_admission",
    "goal": "An ambulance is arriving with a patient; triage in the ER and admit them to a bed.",
    "pipeline": {
        "agents": [
            _agent("ambulance_agent"), _agent("er_agent"),
            _agent("bed_agent"), _agent("staff_agent"),
        ],
        "edges": [
            _edge("ambulance_agent", "er_agent"),
            _edge("er_agent", "bed_agent"),
            _edge("er_agent", "staff_agent"),
        ],
    },
    "expect_agents": ["ambulance_agent", "er_agent", "bed_agent", "staff_agent"],
}

# ── 2. ICU escalation ────────────────────────────────────────────────────────
# A deteriorating ward patient escalated to ICU: census first, then the bed and
# the staffing ratio that the transfer depends on. Fan-out from a single root.
FLOW_ICU_ESCALATION = {
    "name": "icu_escalation",
    "goal": "A ward patient is deteriorating and may need ICU; check ICU capacity and prepare a transfer.",
    "pipeline": {
        "agents": [_agent("icu_agent"), _agent("bed_agent"), _agent("staff_agent")],
        "edges": [
            _edge("icu_agent", "bed_agent"),
            _edge("icu_agent", "staff_agent"),
        ],
    },
    "expect_agents": ["icu_agent", "bed_agent", "staff_agent"],
}

# ── 3. Surgical ──────────────────────────────────────────────────────────────
# A theatre list: OT scheduling drives staffing, the drugs, and the recovery bed.
# The widest fan-out of the themed flows (three parallel agents in level 2).
FLOW_SURGICAL = {
    "name": "surgical",
    "goal": "Schedule tomorrow's theatre list and make sure staff, medication and recovery beds are ready.",
    "pipeline": {
        "agents": [
            _agent("ot_agent"), _agent("staff_agent"),
            _agent("pharmacy_agent"), _agent("bed_agent"),
        ],
        "edges": [
            _edge("ot_agent", "staff_agent"),
            _edge("ot_agent", "pharmacy_agent"),
            _edge("ot_agent", "bed_agent"),
        ],
    },
    "expect_agents": ["ot_agent", "staff_agent", "pharmacy_agent", "bed_agent"],
}

# ── 4. Discharge + billing ───────────────────────────────────────────────────
# The money path, and the only flow with three levels: discharge fans out to
# billing and the bed, then revenue fans IN from billing. Fan-in is where the
# BSP barrier actually earns its keep, so this flow is the one that would catch
# a level-ordering regression.
FLOW_DISCHARGE_BILLING = {
    "name": "discharge_billing",
    "goal": "Discharge the ready patients, raise their bills and check revenue capture.",
    "pipeline": {
        "agents": [
            _agent("discharge_agent"), _agent("billing_agent"),
            _agent("bed_agent"), _agent("revenue_agent"),
        ],
        "edges": [
            _edge("discharge_agent", "billing_agent"),
            _edge("discharge_agent", "bed_agent"),
            _edge("billing_agent", "revenue_agent"),
        ],
    },
    "expect_agents": ["discharge_agent", "billing_agent", "bed_agent", "revenue_agent"],
}

# ── 5. Diagnostics ───────────────────────────────────────────────────────────
# Lab turnaround feeding pharmacy (a result changes the drug) and the ER (a
# critical result changes disposition).
FLOW_DIAGNOSTICS = {
    "name": "diagnostics",
    "goal": "Review lab turnaround for pending samples and act on any critical results.",
    "pipeline": {
        "agents": [_agent("lab_agent"), _agent("pharmacy_agent"), _agent("er_agent")],
        "edges": [
            _edge("lab_agent", "pharmacy_agent"),
            _edge("lab_agent", "er_agent"),
        ],
    },
    "expect_agents": ["lab_agent", "pharmacy_agent", "er_agent"],
}

# ── 6. All agents in one pipeline ────────────────────────────────────────────
# The full-width flow. Four levels, every plannable agent, deliberately shaped so
# level 2 is five agents wide — this is the one that catches cross-agent state
# collisions and level-ordering faults that the narrow themed flows cannot.
FLOW_ALL = {
    "name": "all_agents",
    "goal": ("Full hospital sweep: intake through discharge — triage arrivals, place and staff "
             "them, run diagnostics and theatre, then discharge and bill."),
    "pipeline": {
        "agents": [_agent(a) for a in ALL_PLANNABLE],
        "edges": [
            # level 1 -> 2: intake fans out to everything that acts on a patient
            _edge("ambulance_agent", "er_agent"),
            _edge("er_agent", "bed_agent"),
            _edge("er_agent", "icu_agent"),
            _edge("er_agent", "lab_agent"),
            _edge("er_agent", "ot_agent"),
            _edge("er_agent", "staff_agent"),
            # level 2 -> 3
            _edge("bed_agent", "discharge_agent"),
            _edge("icu_agent", "discharge_agent"),
            _edge("lab_agent", "pharmacy_agent"),
            _edge("ot_agent", "pharmacy_agent"),
            # level 3 -> 4: the money path fans in last
            _edge("discharge_agent", "billing_agent"),
            _edge("pharmacy_agent", "billing_agent"),
            _edge("billing_agent", "revenue_agent"),
        ],
    },
    "expect_agents": list(ALL_PLANNABLE),
}


THEMED_FLOWS = [
    FLOW_ER_ADMISSION,
    FLOW_ICU_ESCALATION,
    FLOW_SURGICAL,
    FLOW_DISCHARGE_BILLING,
    FLOW_DIAGNOSTICS,
]

ALL_FLOWS = THEMED_FLOWS + [FLOW_ALL]

# Convenience for parametrize ids.
FLOW_IDS = [f["name"] for f in ALL_FLOWS]
