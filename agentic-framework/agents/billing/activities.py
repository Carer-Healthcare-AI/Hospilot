import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from workflows.unified_executor import execute as _exec
from temporalio import activity

from api.routes.ws import broadcast
from cache import redis as cache
from db.hasura import hasura
from agents.revenue.finance import repository as fin_repo

logger = logging.getLogger(__name__)


def _session_patient_tokens(raw) -> list[str]:
    """Return all patient_tokens from a session_patient cache value (may be 1 or many)."""
    if not raw:
        return []
    patients = raw if isinstance(raw, list) else [raw]
    return [
        p.get("patient_token") or p.get("token")
        for p in patients
        if isinstance(p, dict) and (p.get("patient_token") or p.get("token"))
    ]


@dataclass
class BillingOptimizationInput:
    session_id: str
    goal: str = ""


# Moved from revenue/activities.py (revenue/billing split 2026-06): single-patient
# invoice lookup and bill-generation execution now belong to billing_agent.
@dataclass
class PatientBillingInput:
    session_id: str
    goal: str


@dataclass
class InitiateBillingInput:
    session_id: str
    goal: str = ""


# -- sa_claim_validation -------------------------------------------------------

@activity.defn
async def detect_claim_discrepancies(session_id: str) -> dict:
    """Detect duplicate claims, amount mismatches, and claims missing invoice linkage."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_claim_validation"})

    raw    = await cache.get(f"session_patient:{session_id}")
    tokens = _session_patient_tokens(raw)
    if tokens:
        invoices = []
        for tok in tokens:
            invoices.extend(await fin_repo.patient_invoices(tok))
        visit_ids = [inv.get("visit_id") for inv in invoices if inv.get("visit_id")]
        claims = await fin_repo.patient_claims(visit_ids) if visit_ids else []
    else:
        claims   = await fin_repo.all_claims()
        invoices = await fin_repo.all_invoices()

    invoice_ids = {inv.get("id") for inv in invoices}
    invoice_by_visit = {}
    for inv in invoices:
        vid = inv.get("visit_id")
        if vid:
            invoice_by_visit.setdefault(vid, []).append(inv)

    discrepancies = []

    # Claims with no matching invoice
    for c in claims:
        vid = c.get("visit_id")
        if vid and vid not in invoice_by_visit:
            discrepancies.append({
                "type":        "missing_invoice",
                "claim_id":    c.get("id"),
                "patient_id":  c.get("patient_id"),
                "amount":      c.get("claim_amount"),
            })

    # Duplicate detection -- same patient + amount + status=pending
    seen: dict = {}
    for c in claims:
        if str(c.get("status", "")).lower() == "pending":
            key = (c.get("patient_id"), str(c.get("claim_amount")))
            if key in seen:
                discrepancies.append({
                    "type":       "duplicate_claim",
                    "claim_id":   c.get("id"),
                    "duplicate_of": seen[key],
                    "amount":     c.get("claim_amount"),
                })
            else:
                seen[key] = c.get("id")

    result = {
        "total_claims":       len(claims),
        "discrepancy_count":  len(discrepancies),
        "missing_invoice":    sum(1 for d in discrepancies if d["type"] == "missing_invoice"),
        "duplicate_claims":   sum(1 for d in discrepancies if d["type"] == "duplicate_claim"),
        "discrepancies":      discrepancies[:20],
    }

    if discrepancies:
        await broadcast(session_id, {
            "type":     "alert",
            "severity": "warning",
            "message":  f"{len(discrepancies)} claim discrepancy(ies) detected -- review required.",
        })

    await broadcast(session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_claim_validation", "result": {
            "discrepancy_count": result["discrepancy_count"],
            "total_claims": result["total_claims"],
        },
    })
    logger.info("detect_claim_discrepancies  session=%s  discrepancies=%d", session_id, len(discrepancies))
    return result


@activity.defn
async def validate_insurance_eligibility(session_id: str) -> dict:
    """Check claims for missing TPA linkage and flag unverified insurance."""
    raw    = await cache.get(f"session_patient:{session_id}")
    tokens = _session_patient_tokens(raw)
    if tokens:
        invoices = []
        for tok in tokens:
            invoices.extend(await fin_repo.patient_invoices(tok))
        visit_ids = [inv.get("visit_id") for inv in invoices if inv.get("visit_id")]
        claims = await fin_repo.patient_claims(visit_ids) if visit_ids else []
    else:
        claims = await fin_repo.all_claims()

    no_tpa         = [c for c in claims if not c.get("tpa_id")]
    pending_claims = [c for c in claims if str(c.get("status", "")).lower() == "pending"]
    no_tpa_pending = [c for c in pending_claims if not c.get("tpa_id")]

    unverified_amount = sum(float(c.get("claim_amount") or 0) for c in no_tpa_pending)

    if no_tpa_pending:
        await broadcast(session_id, {
            "type":     "alert",
            "severity": "warning",
            "message":  (
                f"{len(no_tpa_pending)} pending claim(s) have no TPA assigned -- "
                f"₹{unverified_amount:,.0f} at risk of rejection."
            ),
        })

    result = {
        "total_claims":          len(claims),
        "pending_count":         len(pending_claims),
        "no_tpa_count":          len(no_tpa),
        "no_tpa_pending_count":  len(no_tpa_pending),
        "unverified_amount":     round(unverified_amount, 2),
        "eligibility_issues":    len(no_tpa_pending),
    }
    logger.info("validate_insurance_eligibility  session=%s  issues=%d", session_id, len(no_tpa_pending))
    return result


# NOTE (revenue/billing split 2026-06): denial-risk PREDICTION now lives solely
# in revenue_agent (predict_denial_risk_rev). billing_agent does structural claim
# validation only. Commented out rather than deleted -- remove once stable.
# @activity.defn
# async def predict_denial_risk(session_id: str) -> dict:
#     """Claude assesses denial risk across pending claims and flags high-risk submissions."""
#     await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_denial_prediction"})
#
#     claims  = await fin_repo.all_claims()
#     pending = [c for c in claims if str(c.get("status", "")).lower() == "pending"][:30]
#     if not pending:
#         result = {"high_risk_count": 0, "medium_risk_count": 0, "claims_at_risk": []}
#         await broadcast(session_id, {
#             "type": "sub_agent_completed", "sub_agent": "sa_denial_prediction", "result": result,
#         })
#         return result
#
#     analysis = await _exec(
#         task_id="exec__predict_denial_risk",
#         description=(
#             "Assess denial risk for pending insurance claims. "
#             "HIGH risk: missing tpa_id OR missing visit_id. "
#             "MEDIUM risk: claim_amount is 0 or unusually low (< 500). "
#             "high_risk and medium_risk are lists of {id, reason}. "
#             "summary is one sentence."
#         ),
#         input_schema={
#             "claims": "list of dicts — each has: id (str), claim_amount (float), tpa_id (str or null), visit_id (str or null)",
#         },
#         output_fields=["high_risk", "medium_risk", "summary"],
#         input_data={
#             "claims": [{"id": c.get("id", "")[:8], "claim_amount": c.get("claim_amount", 0),
#                         "tpa_id": c.get("tpa_id"), "visit_id": c.get("visit_id")} for c in pending],
#         },
#     )
#     high_risk   = analysis.get("high_risk", [])
#     medium_risk = analysis.get("medium_risk", [])
#
#     if high_risk:
#         await broadcast(session_id, {
#             "type": "alert", "severity": "critical",
#             "message": f"{len(high_risk)} claim(s) at HIGH denial risk -- review before submission.",
#         })
#
#     result = {
#         "high_risk_count":   len(high_risk),
#         "medium_risk_count": len(medium_risk),
#         "claims_at_risk":    high_risk,
#         "summary":           analysis.get("summary", ""),
#     }
#     await broadcast(session_id, {
#         "type": "sub_agent_completed", "sub_agent": "sa_denial_prediction",
#         "result": {"high_risk_count": result["high_risk_count"]},
#     })
#     logger.info("predict_denial_risk  session=%s  high=%d  medium=%d", session_id, len(high_risk), len(medium_risk))
#     return result


@activity.defn
async def check_billing_compliance(session_id: str) -> dict:
    """Flag claims missing required fields and invoices with incomplete billing codes."""
    raw    = await cache.get(f"session_patient:{session_id}")
    tokens = _session_patient_tokens(raw)
    if tokens:
        invoices = []
        for tok in tokens:
            invoices.extend(await fin_repo.patient_invoices(tok))
        visit_ids = [inv.get("visit_id") for inv in invoices if inv.get("visit_id")]
        claims = await fin_repo.patient_claims(visit_ids) if visit_ids else []
    else:
        claims   = await fin_repo.all_claims()
        invoices = await fin_repo.all_invoices()

    required_claim_fields   = ["patient_id", "visit_id", "tpa_id", "claim_amount"]
    required_invoice_fields = ["patient_id", "invoice_type", "grand_total"]

    claim_issues = []
    for c in claims:
        missing = [f for f in required_claim_fields if not c.get(f)]
        if missing:
            claim_issues.append({"claim_id": c.get("id"), "missing_fields": missing})

    invoice_issues = []
    for inv in invoices:
        missing = [f for f in required_invoice_fields if not inv.get(f)]
        if missing:
            invoice_issues.append({"invoice_id": inv.get("id"), "missing_fields": missing})

    total_issues = len(claim_issues) + len(invoice_issues)

    if total_issues > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{total_issues} billing compliance issue(s) detected -- missing required fields.",
        })

    result = {
        "claim_compliance_issues":   len(claim_issues),
        "invoice_compliance_issues": len(invoice_issues),
        "total_compliance_issues":   total_issues,
        "compliant_claims":          len(claims) - len(claim_issues),
        "compliant_invoices":        len(invoices) - len(invoice_issues),
        "claim_issues":              claim_issues[:10],
        "invoice_issues":            invoice_issues[:10],
    }
    logger.info("check_billing_compliance  session=%s  issues=%d", session_id, total_issues)
    return result


# -- sa_billing_optimization ---------------------------------------------------

@activity.defn
async def track_pending_payments(session_id: str) -> dict:
    """Bucket overdue invoices by SLA and flag high-value overdue accounts."""
    invoices = await fin_repo.all_invoices()
    now      = datetime.now(timezone.utc)

    overdue_sla, overdue_7d, overdue_30d, overdue_90d = [], [], [], []
    for inv in invoices:
        if inv.get("payment_status") not in ("Unpaid", "Partial"):
            continue
        due_raw = inv.get("due_date") or inv.get("invoice_date") or ""
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
            days_overdue = (now - due).days
        except Exception:
            days_overdue = 0

        if days_overdue > 0:
            entry = {**inv, "days_overdue": days_overdue}
            overdue_sla.append(entry)
            if days_overdue > 90:
                overdue_90d.append(entry)
            elif days_overdue > 30:
                overdue_30d.append(entry)
            elif days_overdue > 7:
                overdue_7d.append(entry)

    high_value = sorted(
        [i for i in overdue_sla if float(i.get("balance") or 0) > 50000],
        key=lambda x: float(x.get("balance") or 0), reverse=True,
    )

    overdue_amount = sum(float(i.get("balance") or 0) for i in overdue_sla)

    if high_value:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (
                f"{len(high_value)} high-value overdue invoice(s) -- "
                f"₹{sum(float(i.get('balance') or 0) for i in high_value):,.0f} outstanding."
            ),
        })

    result = {
        "overdue_count":    len(overdue_sla),
        "overdue_amount":   round(overdue_amount, 2),
        "overdue_7d":       len(overdue_7d),
        "overdue_30d":      len(overdue_30d),
        "overdue_90d":      len(overdue_90d),
        "high_value_count": len(high_value),
        "high_value_invoices": high_value[:5],
    }
    logger.info("track_pending_payments  session=%s  overdue=%d  amount=%.2f", session_id, len(overdue_sla), overdue_amount)
    return result


@activity.defn
async def detect_revenue_leakage(session_id: str) -> dict:
    """Find IPD admissions with no invoice raised and procedures without billing entries."""
    invoices   = await fin_repo.all_invoices()
    billed_ids = {inv.get("admission_id") for inv in invoices if inv.get("admission_id")}

    # Find admissions from the invoice set itself that have very low balances vs IPD norms
    ipd_invoices = [i for i in invoices if i.get("invoice_type") == "IPD"]
    low_value_ipd = [
        i for i in ipd_invoices
        if float(i.get("grand_total") or 0) < 1000 and i.get("admission_id")
    ]

    # Claims without any invoice linkage
    claims = await fin_repo.all_claims()
    unlinked_claims = [c for c in claims if c.get("visit_id") and not any(
        inv.get("visit_id") == c.get("visit_id") for inv in invoices
    )]

    estimated_leakage = sum(float(c.get("claim_amount") or 0) for c in unlinked_claims)

    if estimated_leakage > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (
                f"Revenue leakage detected -- {len(unlinked_claims)} claim(s) with no invoice "
                f"(₹{estimated_leakage:,.0f} at risk)."
            ),
        })

    result = {
        "unlinked_claims_count":   len(unlinked_claims),
        "estimated_leakage":       round(estimated_leakage, 2),
        "low_value_ipd_count":     len(low_value_ipd),
        "leakage_detected":        len(unlinked_claims) > 0 or len(low_value_ipd) > 0,
    }
    logger.info("detect_revenue_leakage  session=%s  unlinked=%d  leakage=%.2f", session_id, len(unlinked_claims), estimated_leakage)
    return result


@activity.defn
async def generate_billing_recommendations(inp: BillingOptimizationInput) -> dict:
    """Claude synthesises billing state and generates prioritised optimisation recommendations."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_billing_recommendations"})

    invoices = await fin_repo.all_invoices()
    claims   = await fin_repo.all_claims()

    outstanding_count  = sum(1 for i in invoices if i.get("payment_status") in ("Unpaid", "Partial"))
    outstanding_amount = sum(float(i.get("balance") or 0) for i in invoices if i.get("payment_status") in ("Unpaid", "Partial"))
    denied_count       = sum(1 for c in claims if str(c.get("status", "")).lower() in ("denied", "rejected"))
    denied_amount      = sum(float(c.get("claim_amount") or 0) for c in claims if str(c.get("status", "")).lower() in ("denied", "rejected"))

    analysis = await _exec(
        task_id="exec__generate_billing_recommendations",
        description=(
            "Generate 3-5 billing optimisation recommendations based on outstanding invoices and denied claims. "
            "If denied_count > 10: priority 1 action = initiate bulk claim resubmission. "
            "If outstanding_count > 20: priority 1/2 action = escalate collections follow-up. "
            "If denied_amount > 500000: flag for senior review. "
            "recommendations is list of {priority (int), action (str), expected_impact (str), effort ('low'|'medium'|'high')}. "
            "summary is one sentence overall assessment."
        ),
        input_schema={
            "outstanding_count": "int — unpaid or partial invoices",
            "outstanding_amount": "float — total outstanding amount in INR",
            "denied_count": "int — denied or rejected claims",
            "denied_amount": "float — total denied claim amount in INR",
        },
        output_fields=["recommendations", "summary"],
        input_data={
            "outstanding_count": outstanding_count,
            "outstanding_amount": outstanding_amount,
            "denied_count": denied_count,
            "denied_amount": denied_amount,
        },
    )

    result = {
        "recommendation_count": len(analysis.get("recommendations", [])),
        "recommendations":      analysis.get("recommendations", []),
        "summary":              analysis.get("summary", ""),
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_billing_recommendations",
        "result": {"recommendation_count": result["recommendation_count"], "summary": result["summary"]},
    })
    logger.info("generate_billing_recommendations  session=%s  count=%d", inp.session_id, result["recommendation_count"])
    return result


@activity.defn
async def prioritize_payments(session_id: str) -> dict:
    """Rank outstanding invoices by value × aging score for targeted collection."""
    invoices = await fin_repo.all_invoices()
    now      = datetime.now(timezone.utc)

    scored = []
    for inv in invoices:
        if inv.get("payment_status") not in ("Unpaid", "Partial"):
            continue
        balance = float(inv.get("balance") or 0)
        if balance <= 0:
            continue
        due_raw = inv.get("due_date") or inv.get("invoice_date") or ""
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
            age_days = max((now - due).days, 0)
        except Exception:
            age_days = 0

        # Score = balance * aging_multiplier (older = higher urgency)
        aging_mult = 1 + min(age_days / 30, 3)
        scored.append({
            "invoice_id":     inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "patient_token":  inv.get("patient_token"),
            "balance":        round(balance, 2),
            "days_overdue":   age_days,
            "priority_score": round(balance * aging_mult, 2),
        })

    scored.sort(key=lambda x: x["priority_score"], reverse=True)

    result = {
        "prioritized_count": len(scored),
        "total_recoverable": round(sum(s["balance"] for s in scored), 2),
        "top_priority":      scored[:10],
    }
    logger.info("prioritize_payments  session=%s  count=%d  recoverable=%.2f", session_id, len(scored), result["total_recoverable"])
    return result


# NOTE (revenue/billing split 2026-06): billing's sa_denial_prevention is retired
# -- denial PREDICTION + PREVENTION now live solely in revenue_agent
# (sa_rev_denial_prevention). These action tasks were gated off the removed
# ta_predict_denial_risk / eligibility outputs. Commented out rather than deleted
# -- remove once stable. (Also retires the sa_denial_prediction / sa_stricter_validation
# / sa_claim_escalation broadcast names.)
# -- sa_denial_prevention ------------------------------------------------------
#
# @activity.defn
# async def trigger_presubmission_review(session_id: str) -> dict:
#     """Trigger pre-submission review for high-denial-risk claims."""
#     await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_denial_prevention"})
#     claims  = await fin_repo.all_claims()
#     pending = [c for c in claims if str(c.get("status", "")).lower() == "pending"]
#     high_risk = [c for c in pending if not c.get("tpa_id") or not c.get("visit_id")]
#     for claim in high_risk[:10]:
#         await broadcast(session_id, {
#             "type": "alert", "severity": "warning",
#             "message": f"Pre-submission review required: claim {str(claim.get('id', ''))[:8]} -- denial risk factors detected.",
#         })
#     if high_risk:
#         await hasura.write_audit(
#             session_id=session_id, agent_id="billing_agent",
#             event_type="presubmission_review_triggered",
#             payload={"count": len(high_risk), "claim_ids": [c.get("id") for c in high_risk[:20]]},
#         )
#     result = {"reviewed": len(high_risk)}
#     await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_denial_prevention", "result": result})
#     logger.info("trigger_presubmission_review  session=%s  reviewed=%d", session_id, len(high_risk))
#     return result
#
#
# @activity.defn
# async def apply_stricter_validation(session_id: str) -> dict:
#     """Apply stricter TPA/insurance validation to risky payer claims."""
#     await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_stricter_validation"})
#     claims    = await fin_repo.all_claims()
#     no_tpa    = [c for c in claims if str(c.get("status", "")).lower() == "pending" and not c.get("tpa_id")]
#     flagged   = []
#     for claim in no_tpa:
#         amount = float(claim.get("claim_amount") or 0)
#         if amount > 10000:
#             flagged.append(claim)
#             await broadcast(session_id, {
#                 "type": "alert", "severity": "warning",
#                 "message": f"Stricter validation applied: claim {str(claim.get('id', ''))[:8]} -- no TPA, high value ₹{amount:,.0f}.",
#             })
#     if flagged:
#         await hasura.write_audit(
#             session_id=session_id, agent_id="billing_agent",
#             event_type="stricter_validation_applied",
#             payload={"count": len(flagged)},
#         )
#     result = {"applied": len(flagged)}
#     await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_stricter_validation", "result": result})
#     logger.info("apply_stricter_validation  session=%s  applied=%d", session_id, len(flagged))
#     return result
#
#
# @activity.defn
# async def escalate_claim_review_priority(session_id: str) -> dict:
#     """Escalate claim review priority for high-financial-exposure items."""
#     await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_claim_escalation"})
#     claims    = await fin_repo.all_claims()
#     pending   = [c for c in claims if str(c.get("status", "")).lower() == "pending"]
#     high_exp  = [c for c in pending if float(c.get("claim_amount") or 0) > 50000]
#     for claim in high_exp[:10]:
#         await broadcast(session_id, {
#             "type": "alert", "severity": "critical",
#             "message": f"HIGH EXPOSURE: claim {str(claim.get('id', ''))[:8]} -- ₹{float(claim.get('claim_amount', 0)):,.0f} -- escalated for priority review.",
#         })
#     if high_exp:
#         await hasura.write_audit(
#             session_id=session_id, agent_id="billing_agent",
#             event_type="claim_review_escalated",
#             payload={"count": len(high_exp), "total_exposure": sum(float(c.get("claim_amount") or 0) for c in high_exp)},
#         )
#     result = {"escalated": len(high_exp)}
#     await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_claim_escalation", "result": result})
#     logger.info("escalate_claim_review_priority  session=%s  escalated=%d", session_id, len(high_exp))
#     return result


# -- sa_billing_optimization -- action tasks ------------------------------------

@activity.defn
async def trigger_payment_reminder(session_id: str) -> dict:
    """Send payment reminders for overdue invoices."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_payment_reminder"})
    invoices = await fin_repo.all_invoices()
    now = datetime.now(timezone.utc)
    reminders = []
    for inv in invoices:
        if inv.get("payment_status") not in ("Unpaid", "Partial"):
            continue
        due_raw = inv.get("due_date") or inv.get("invoice_date") or ""
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
            days_overdue = (now - due).days
        except Exception:
            days_overdue = 0
        if days_overdue > 7:
            reminders.append(inv)
            await broadcast(session_id, {
                "type": "alert", "severity": "warning",
                "message": f"Payment reminder: invoice {inv.get('invoice_number', inv.get('id', '')[:8])} -- ₹{float(inv.get('balance') or 0):,.0f} overdue {days_overdue}d.",
            })
    if reminders:
        await hasura.write_audit(
            session_id=session_id, agent_id="billing_agent",
            event_type="payment_reminders_sent",
            payload={"count": len(reminders)},
        )
    result = {"reminders_sent": len(reminders)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_payment_reminder", "result": result})
    logger.info("trigger_payment_reminder  session=%s  reminders=%d", session_id, len(reminders))
    return result


@activity.defn
async def notify_followup_team(session_id: str) -> dict:
    """Notify the follow-up team about claims awaiting payer response."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_followup_notify"})
    claims     = await fin_repo.all_claims()
    no_tpa_pending = [c for c in claims if str(c.get("status", "")).lower() == "pending" and not c.get("tpa_id")]
    if no_tpa_pending:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Follow-up team notification: {len(no_tpa_pending)} claim(s) awaiting payer response -- no TPA assigned.",
            "count": len(no_tpa_pending),
        })
        await hasura.write_audit(
            session_id=session_id, agent_id="billing_agent",
            event_type="followup_team_notified",
            payload={"count": len(no_tpa_pending), "claim_ids": [c.get("id") for c in no_tpa_pending[:20]]},
        )
    result = {"notified": len(no_tpa_pending)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_followup_notify", "result": result})
    logger.info("notify_followup_team  session=%s  notified=%d", session_id, len(no_tpa_pending))
    return result


# -- sa_rev_patient_billing (moved from revenue/activities.py) ------------------
# Subagent id keeps the sa_rev_* prefix by design -- renaming churns registry rows
# and code references for no functional gain (see migration 026).

@activity.defn
async def get_patient_billing(inp: PatientBillingInput) -> dict:
    """
    Extract a patient token from the goal text, then fetch all invoices
    and claims for that patient from Hasura.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_rev_patient_billing",
    })

    raw    = await cache.get(f"session_patient:{inp.session_id}")
    tokens = _session_patient_tokens(raw)
    patient_token = tokens[0] if tokens else None
    if not patient_token:
        match = re.search(r'\b([A-Z]{2,}-?\d{4,}|\d{6,})\b', inp.goal)
        patient_token = match.group(1) if match else None

    if not patient_token:
        result = {
            "patient_token": None,
            "error": "Could not identify patient from query -- please include the patient token or ID",
        }
        await broadcast(inp.session_id, {
            "type": "sub_agent_completed",
            "sub_agent": "sa_rev_patient_billing",
            "result": result,
        })
        return result

    invoices = await fin_repo.patient_invoices(patient_token)

    visit_ids = [inv["visit_id"] for inv in invoices if inv.get("visit_id")]
    claims    = await fin_repo.patient_claims(visit_ids)

    total_billed = sum(float(inv.get("grand_total")  or 0) for inv in invoices)
    total_paid   = sum(float(inv.get("paid_amount")  or 0) for inv in invoices)
    outstanding  = round(total_billed - total_paid, 2)

    unpaid_invoices = [i for i in invoices if i.get("payment_status") in ("Unpaid", "Partial")]
    pending_claims  = [c for c in claims if str(c.get("status", "")).lower() == "pending"]
    denied_claims   = [c for c in claims if str(c.get("status", "")).lower() in ("denied", "rejected")]

    if denied_claims:
        denied_amt = sum(float(c.get("claim_amount") or 0) for c in denied_claims)
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "warning",
            "message": (
                f"Patient {patient_token}: {len(denied_claims)} denied claim(s) -- "
                f"₹{denied_amt:,.0f} at risk. Resubmission required."
            ),
        })

    result = {
        "patient_token":  patient_token,
        "invoice_count":  len(invoices),
        "total_billed":   round(total_billed, 2),
        "total_paid":     round(total_paid, 2),
        "outstanding":    outstanding,
        "unpaid_count":   len(unpaid_invoices),
        "claims_count":   len(claims),
        "claims_pending": len(pending_claims),
        "claims_denied":  len(denied_claims),
        "invoices":       invoices[:10],
    }

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_rev_patient_billing",
        "result": result,
    })
    logger.info(
        "patient billing  session=%s  patient=%s  invoices=%d  outstanding=%.2f",
        inp.session_id, patient_token, len(invoices), outstanding,
    )
    return result


# -- sa_rev_initiate_billing (moved from revenue/activities.py) -----------------

def _resolved_patients(raw, goal: str) -> list[dict]:
    """Normalise the ``session_patient:{sid}`` cache into a list of patient contexts.

    Falls back to a single token parsed from the goal text when no patient was
    resolved by patient verification (same regex the lookup task uses)."""
    if isinstance(raw, list):
        patients = [p for p in raw if isinstance(p, dict)]
    elif isinstance(raw, dict):
        patients = [raw]
    else:
        patients = []
    if patients:
        return patients
    match = re.search(r'\b([A-Z]{2,}-?\d{4,}|\d{6,})\b', goal or "")
    if match:
        tok = match.group(1)
        return [{"token": tok, "patient_token": tok}]
    return []


async def _scope_to_admitted(patients: list[dict]) -> tuple[list[dict], list[str]]:
    """G46: keep only patients confirmed for admission -- a billing episode must
    not be opened for patients merely resolved as incoming (e.g. 22 pediatric ER
    children still waiting for triage). The authoritative signal is an IPD
    admission record for the patient_token; any status counts (admitted /
    discharging / discharged) so discharge-time billing still works. A resolved
    patient with no admission record is excluded. This runs only for a session
    cohort -- an explicitly named single patient (goal token) is billed as-is."""
    admitted_tokens = {a.get("patient_token") for a in await cache.get_all_admissions()}
    admitted_tokens.discard(None)
    kept: list[dict] = []
    excluded: list[str] = []
    for p in patients:
        token = p.get("patient_token") or p.get("token")
        if token in admitted_tokens:
            kept.append(p)
        else:
            excluded.append(token)
    return kept, excluded


@activity.defn
async def create_billing_request(inp: InitiateBillingInput) -> dict:
    """Create a bill-generation request for the resolved patient(s).

    This does NOT write to the HIS directly. It STAGES the request(s) in Redis --
    exactly like discharge/appointment staging -- so nothing is billed until the
    user commits. When the session is committed (the recommendation is pushed to
    the HIS), ``commit_session`` inserts these into ``hospilot.billing_requests``
    for the DB side to turn into actual bills.

    We never fabricate amounts or line items here: the request carries only patient
    and encounter references plus ``generate_from_charges``; the DB side assembles
    the bill from the patient's recorded charges.
    """
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_rev_initiate_billing"})

    raw = await cache.get(f"session_patient:{inp.session_id}")
    patients = _resolved_patients(raw, inp.goal)

    # G46: a session cohort (patients resolved via patient verification this run)
    # is scoped to admission-confirmed patients only. The explicit single-patient
    # path (no session cohort; token parsed from the goal) is a direct instruction
    # to bill a named patient, so it is billed as-is.
    excluded_tokens: list[str] = []
    if raw:
        patients, excluded_tokens = await _scope_to_admitted(patients)

    requests: list[dict] = []
    for p in patients:
        token = p.get("patient_token") or p.get("token")
        if not token:
            continue
        requests.append({
            "patient_token":         token,
            "patient_name":          p.get("patient_name"),
            "uhid":                  p.get("uhid"),
            "visit_id":              p.get("current_visit_id") or p.get("visit_id"),
            "admission_id":          p.get("admission_id"),
            "invoice_type":          p.get("invoice_type") or "IPD",
            "generate_from_charges": True,
            "source":                "initiate_billing",
        })

    if not requests:
        if excluded_tokens:
            result = {
                "status":              "no_admitted_patient",
                "patient_count":       0,
                "excluded_unadmitted": len(excluded_tokens),
                "billing_requests":    [],
                "error": f"No patient confirmed for admission -- billing is only initiated for admitted patients ({len(excluded_tokens)} resolved patient(s) not yet admitted were skipped).",
            }
            log_msg = "all %d resolved patient(s) unadmitted -- nothing billed" % len(excluded_tokens)
        else:
            result = {
                "status":           "no_patient",
                "patient_count":    0,
                "billing_requests": [],
                "error": "No resolved patient to bill -- run patient verification or include a patient token in the goal.",
            }
            log_msg = "no resolved patient"
        await broadcast(inp.session_id, {
            "type": "sub_agent_completed", "sub_agent": "sa_rev_initiate_billing", "result": result,
        })
        logger.info("create_billing_request  session=%s  %s", inp.session_id, log_msg)
        return result

    # Stage for the commit step (HIS push). Mirrors discharge/appointment staging.
    await cache.stage(inp.session_id, "billing", requests)

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="billing_agent",
        event_type="billing_request_staged",
        payload={"patient_count": len(requests), "tokens": [r["patient_token"] for r in requests]},
    )

    result = {
        "status":              "staged",
        "patient_count":       len(requests),
        "excluded_unadmitted": len(excluded_tokens),
        "patients_billed":     [r["patient_token"] for r in requests],
        "billing_requests":    requests,
    }
    await broadcast(inp.session_id, {
        "type":          "billing_request_created",
        "sub_agent":     "sa_rev_initiate_billing",
        "patient_count": len(requests),
        "patients":      [r["patient_token"] for r in requests],
        "status":        "staged",
    })
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_rev_initiate_billing",
        "result": {"patient_count": len(requests), "status": "staged"},
    })
    logger.info("create_billing_request  session=%s  patients=%d  excluded_unadmitted=%d  staged",
                inp.session_id, len(requests), len(excluded_tokens))
    return result


# -- sa_billing_backlog --------------------------------------------------------

# -- sa_billing_delay ----------------------------------------------------------

# -- sa_billing_workload -------------------------------------------------------

_WORKLOAD_DAYS = {"6h": 0.25, "12h": 0.5, "24h": 1.0, "3d": 3.0, "7d": 7.0}

