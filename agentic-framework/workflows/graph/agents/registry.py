"""Maps base agent id -> ported async body. Mirrors the old api.agents._WORKFLOW_MAP.

Agents absent from this map (e.g. infection_control_agent, supply_chain_agent)
fall through to a completed stub in graph.nodes._dispatch_body. appointment_agent
(and any other DB-registry agent) routes to the generic registry body.
"""

from workflows.graph.agents.bed import run_bed_body, bid_bed
from workflows.graph.agents.clinical import run_icu_body, run_staff_body
from workflows.graph.agents.simple import (
    run_er_body, run_bed_prediction_body, run_revenue_body,
    run_patient_verification_body,
)

AGENT_BODIES = {
    "bed_agent":            run_bed_body,
    "er_agent":             run_er_body,
    "icu_agent":            run_icu_body,
    "staff_agent":          run_staff_body,
    "bed_prediction_agent": run_bed_prediction_body,
    "revenue_agent":        run_revenue_body,
    "patient_verification_agent": run_patient_verification_body,
}

# Optional per-agent bid hooks for the 'bidding' execution strategy
# (services.strategies). An agent absent here bids a neutral 0.0 (abstains), so
# bidding works across any flow without every agent implementing a hook. The bid
# VALUE is agent-specific; the interface (sid, ctx) -> {"score": float, ...} is
# uniform. See graph.nodes._bid_for.
AGENT_BID_HOOKS = {
    "bed_agent": bid_bed,
}
