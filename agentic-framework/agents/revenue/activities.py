import logging
from dataclasses import dataclass

# import re  # (revenue/billing split 2026-06) only used by the moved patient-billing code

from workflows.unified_executor import execute as _exec
from temporalio import activity

from api.routes.ws import broadcast
from cache import redis as cache
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
class RevAnalysisInput:
    session_id: str
    goal: str = ""


# NOTE (revenue/billing split 2026-06): single-patient invoice lookup and
# bill-generation execution moved to billing_agent. PatientBillingInput /
# InitiateBillingInput now live in agents/billing/activities.py. Commented out
# rather than deleted -- remove once stable.
# @dataclass
# class PatientBillingInput:
#     session_id: str
#     goal: str
#
#
# @dataclass
# class InitiateBillingInput:
#     session_id: str
#     goal: str = ""


# -- sa_rev_optimization -------------------------------------------------------

@activity.defn
async def identify_revenue_leakage(session_id: str) -> dict:
    """Identify revenue leakage: unlinked claims, unbilled IPD, low-value procedures."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_rev_optimization"})

    invoices = await fin_repo.all_invoices()
    claims   = await fin_repo.all_claims()

    invoice_visit_ids = {inv.get("visit_id") for inv in invoices if inv.get("visit_id")}
    unlinked_claims   = [c for c in claims if c.get("visit_id") and c["visit_id"] not in invoice_visit_ids]
    leakage_amount    = sum(float(c.get("claim_amount") or 0) for c in unlinked_claims)

    low_value_ipd = [
        i for i in invoices
        if i.get("invoice_type") == "IPD" and float(i.get("grand_total") or 0) < 1000
    ]

    if leakage_amount > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Revenue leakage: {len(unlinked_claims)} unlinked claim(s) -- ₹{leakage_amount:,.0f} at risk.",
        })

    result = {
        "leakage_detected":    len(unlinked_claims) > 0 or len(low_value_ipd) > 0,
        "leakage_amount":      round(leakage_amount, 2),
        "unbilled_count":      len(unlinked_claims),
        "unlinked_claims":     unlinked_claims[:10],
        "low_value_ipd_count": len(low_value_ipd),
    }
    await broadcast(session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_rev_optimization",
        "result": {"leakage_amount": result["leakage_amount"], "unbilled_count": result["unbilled_count"]},
    })
    logger.info(
        "identify_revenue_leakage  session=%s  unlinked=%d  leakage=%.2f",
        session_id, result["unbilled_count"], leakage_amount,
    )
    return result


@activity.defn
async def optimize_package_utilization(inp: RevAnalysisInput) -> dict:
    """Claude analyses invoice patterns to recommend package billing optimizations."""
    invoices = await fin_repo.all_invoices()

    ipd = [i for i in invoices if i.get("invoice_type") == "IPD"]
    opd = [i for i in invoices if i.get("invoice_type") == "OPD"]
    ipd_avg = sum(float(i.get("grand_total") or 0) for i in ipd) / max(len(ipd), 1)
    opd_avg = sum(float(i.get("grand_total") or 0) for i in opd) / max(len(opd), 1)

    analysis = await _exec(
        task_id="exec__optimize_package_utilization",
        description=(
            "Recommend package billing optimisations based on IPD/OPD invoice averages. "
            "If ipd_avg < 5000: recommend reviewing IPD package pricing. "
            "If opd_count > ipd_count * 3: recommend converting frequent OPD patients to packages. "
            "packages_reviewed = ipd_count + opd_count. "
            "savings_identified is estimated savings as float. "
            "recommendations is list of {priority (int), action (str), expected_impact (str)}."
        ),
        input_schema={
            "ipd_count": "int", "ipd_avg": "float — average IPD invoice amount in INR",
            "opd_count": "int", "opd_avg": "float — average OPD invoice amount in INR",
        },
        output_fields=["packages_reviewed", "savings_identified", "recommendations"],
        input_data={"ipd_count": len(ipd), "ipd_avg": ipd_avg, "opd_count": len(opd), "opd_avg": opd_avg},
    )

    result = {
        "packages_reviewed":  int(analysis.get("packages_reviewed", 0)),
        "savings_identified": float(analysis.get("savings_identified", 0.0)),
        "recommendations":    analysis.get("recommendations", []),
    }
    logger.info(
        "optimize_package_utilization  session=%s  savings=%.2f",
        inp.session_id, result["savings_identified"],
    )
    return result


@activity.defn
async def analyze_resource_utilization(session_id: str) -> dict:
    """Analyze bed, OT, and equipment utilization efficiency across hospital resources."""
    all_beds     = await cache.get_all_beds()
    admissions   = await cache.get_all_admissions()
    active_beds  = [b for b in all_beds if b.get("is_active")]
    avail_beds   = [b for b in active_beds if b.get("status") == "Available"]
    icu_beds     = [b for b in active_beds if "ICU" in (b.get("ward") or "").upper()]
    icu_avail    = [b for b in icu_beds if b.get("status") == "Available"]
    total        = len(active_beds)
    avail_count  = len(avail_beds)
    icu_total    = len(icu_beds)
    beds_summary = {
        "total_beds":           total,
        "occupied_beds":        total - avail_count,
        "available_beds":       avail_count,
        "occupancy_pct":        round((total - avail_count) / max(total, 1) * 100),
        "icu_total":            icu_total,
        "icu_occupied":         icu_total - len(icu_avail),
        "icu_available":        len(icu_avail),
        "ventilated_available": sum(1 for b in icu_avail if b.get("ventilation") == "full_ventilator"),
    }

    total_beds     = beds_summary.get("total", 0)
    occupied_beds  = beds_summary.get("occupied", 0)
    available_beds = beds_summary.get("available", 0)
    utilization_score = round((occupied_beds / max(total_beds, 1)) * 100, 1)

    idle_equipment_count = max(available_beds - len(admissions), 0)

    bottlenecks = []
    if utilization_score > 90:
        bottlenecks.append("Bed occupancy critically high -- admission backlog risk")
    elif utilization_score < 60:
        bottlenecks.append("Low bed utilization -- review ward allocation strategy")

    result = {
        "utilization_score":    utilization_score,
        "total_beds":           total_beds,
        "occupied_beds":        occupied_beds,
        "available_beds":       available_beds,
        "idle_equipment_count": idle_equipment_count,
        "bottlenecks":          bottlenecks,
    }
    logger.info(
        "analyze_resource_utilization  session=%s  utilization=%.1f%%",
        session_id, utilization_score,
    )
    return result


@activity.defn
async def analyze_dept_profitability(session_id: str) -> dict:
    """Analyze revenue contribution and profitability across departments."""
    invoices    = await fin_repo.all_invoices()
    departments = await cache.get_all_departments()
    admissions  = await cache.get_all_admissions()

    admission_dept: dict[str, str] = {
        a.get("id", ""): a.get("ward", "") or a.get("department_id", "")
        for a in admissions
    }
    dept_revenue: dict[str, float] = {}
    for inv in invoices:
        dept = admission_dept.get(inv.get("admission_id", ""), "Unknown")
        dept_revenue[dept] = dept_revenue.get(dept, 0.0) + float(inv.get("grand_total") or 0)

    dept_count         = len(departments)
    below_target_count = sum(1 for rev in dept_revenue.values() if rev < 50_000)

    recommendations = []
    if below_target_count > 0:
        recommendations.append({
            "priority": 1,
            "action": f"{below_target_count} department(s) below revenue threshold -- review billing capture",
            "expected_impact": "Revenue recovery",
        })

    result = {
        "dept_count":         dept_count,
        "below_target_count": below_target_count,
        "dept_revenue":       dict(sorted(dept_revenue.items(), key=lambda x: x[1], reverse=True)[:10]),
        "recommendations":    recommendations,
    }
    await broadcast(session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_rev_optimization",
        "result": {"dept_count": dept_count, "below_target_count": below_target_count},
    })
    logger.info(
        "analyze_dept_profitability  session=%s  depts=%d  below_target=%d",
        session_id, dept_count, below_target_count,
    )
    return result


# -- sa_rev_denial_prevention --------------------------------------------------

@activity.defn
async def predict_denial_risk_rev(session_id: str) -> dict:
    """Claude predicts denial risk for pending revenue claims before submission."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_rev_denial_prevention"})

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
    pending = [c for c in claims if str(c.get("status", "")).lower() == "pending"][:30]

    if not pending:
        result = {"high_risk_count": 0, "medium_risk_count": 0, "denial_probability": 0.0, "claims_at_risk": []}
        await broadcast(session_id, {
            "type": "sub_agent_completed", "sub_agent": "sa_rev_denial_prevention", "result": result,
        })
        return result

    claims_text = "\n".join(
        f"- Claim {c.get('id', '')[:8]}: ₹{c.get('claim_amount', 0):,.0f}, "
        f"TPA={c.get('tpa_id') or 'NONE'}, visit={c.get('visit_id', '')[:8] if c.get('visit_id') else 'NONE'}"
        for c in pending
    )

    analysis = await _exec(
        task_id="exec__predict_denial_risk",
        description=(
            "Assess denial risk for pending insurance claims. "
            "HIGH risk: missing tpa_id OR missing visit_id. "
            "MEDIUM risk: claim_amount is 0 or unusually low (< 500). "
            "denial_probability = high_risk_count / max(total_claims, 1). "
            "high_risk and medium_risk are lists of {id, reason}. summary is one sentence."
        ),
        input_schema={
            "claims": "list of dicts — each has: id (str), claim_amount (float), tpa_id (str or null), visit_id (str or null)",
        },
        output_fields=["high_risk", "medium_risk", "denial_probability", "summary"],
        input_data={
            "claims": [{"id": c.get("id", "")[:8], "claim_amount": c.get("claim_amount", 0),
                        "tpa_id": c.get("tpa_id"), "visit_id": c.get("visit_id")} for c in pending],
        },
    )

    high_risk   = analysis.get("high_risk", [])
    medium_risk = analysis.get("medium_risk", [])

    if high_risk:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"{len(high_risk)} claim(s) at HIGH denial risk -- review before submission.",
        })

    result = {
        "high_risk_count":    len(high_risk),
        "medium_risk_count":  len(medium_risk),
        "denial_probability": float(analysis.get("denial_probability", 0.0)),
        "claims_at_risk":     high_risk,
        "summary":            analysis.get("summary", ""),
    }
    await broadcast(session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_rev_denial_prevention",
        "result": {"high_risk_count": result["high_risk_count"], "denial_probability": result["denial_probability"]},
    })
    logger.info(
        "predict_denial_risk_rev  session=%s  high=%d  medium=%d",
        session_id, len(high_risk), len(medium_risk),
    )
    return result


@activity.defn
async def presubmission_validation_rev(session_id: str) -> dict:
    """Validate pending claims for mandatory fields before payer submission."""
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

    required_fields  = ["patient_id", "visit_id", "tpa_id", "claim_amount"]
    issues           = []
    missing_fields_count = 0

    for c in claims:
        if str(c.get("status", "")).lower() != "pending":
            continue
        missing = [f for f in required_fields if not c.get(f)]
        if missing:
            issues.append({"claim_id": c.get("id"), "missing_fields": missing})
            missing_fields_count += len(missing)

    validation_passed = len(issues) == 0

    if not validation_passed:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(issues)} claim(s) failed pre-submission validation -- {missing_fields_count} missing fields.",
        })

    result = {
        "validation_passed":   validation_passed,
        "issues_found":        len(issues),
        "missing_fields_count": missing_fields_count,
        "failed_claims":       issues[:10],
    }
    logger.info("presubmission_validation_rev  session=%s  issues=%d", session_id, len(issues))
    return result


@activity.defn
async def payer_rule_compliance_rev(session_id: str) -> dict:
    """Validate claims against payer-specific policies: TPA linkage and pre-authorisation."""
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
    pending = [c for c in claims if str(c.get("status", "")).lower() == "pending"]

    no_tpa         = [c for c in pending if not c.get("tpa_id")]
    no_preauth     = [c for c in pending if not c.get("pre_auth_number") and c.get("tpa_id")]
    compliance_issues  = len(no_tpa) + len(no_preauth)
    auth_missing_count = len(no_preauth)

    if compliance_issues > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (
                f"{compliance_issues} payer compliance issue(s): "
                f"{len(no_tpa)} missing TPA, {len(no_preauth)} missing pre-auth."
            ),
        })

    result = {
        "compliance_issues":   compliance_issues,
        "no_tpa_count":        len(no_tpa),
        "auth_missing_count":  auth_missing_count,
        "non_covered_count":   0,
        "pending_reviewed":    len(pending),
    }
    logger.info("payer_rule_compliance_rev  session=%s  issues=%d", session_id, compliance_issues)
    return result


@activity.defn
async def detect_missing_docs_rev(session_id: str) -> dict:
    """Detect missing discharge summaries and unsigned documentation before claim submission."""
    raw    = await cache.get(f"session_patient:{session_id}")
    tokens = _session_patient_tokens(raw)
    admissions = await cache.get_all_admissions()
    if tokens:
        token_set  = set(tokens)
        admissions = [a for a in admissions if (a.get("patient_token") or a.get("token")) in token_set]
    discharge_summaries = await cache.get_all_discharge_summaries()
    if tokens:
        scoped_ids          = {a.get("id") for a in admissions if a.get("id")}
        discharge_summaries = [s for s in discharge_summaries if s.get("admission_id") in scoped_ids]

    summary_admission_ids = {s.get("admission_id") for s in discharge_summaries if s.get("admission_id")}
    missing_summaries = sum(
        1 for a in admissions
        if a.get("id") and a["id"] not in summary_admission_ids
    )
    missing_signatures = sum(
        1 for s in discharge_summaries
        if not s.get("signed_by") and not s.get("finalized_at")
    )
    missing_docs_count = missing_summaries + missing_signatures

    if missing_docs_count > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (
                f"{missing_docs_count} documentation gap(s): "
                f"{missing_summaries} missing summaries, {missing_signatures} unsigned."
            ),
        })

    result = {
        "missing_docs_count": missing_docs_count,
        "missing_summaries":  missing_summaries,
        "missing_signatures": missing_signatures,
        "admissions_checked": len(admissions),
    }
    logger.info("detect_missing_docs_rev  session=%s  missing=%d", session_id, missing_docs_count)
    return result


@activity.defn
async def escalation_recommendations_rev(inp: RevAnalysisInput) -> dict:
    """Claude synthesises denial risk and payer patterns into prioritised escalation actions."""
    raw    = await cache.get(f"session_patient:{inp.session_id}")
    tokens = _session_patient_tokens(raw)
    if tokens:
        invoices = []
        for tok in tokens:
            invoices.extend(await fin_repo.patient_invoices(tok))
        visit_ids = [inv.get("visit_id") for inv in invoices if inv.get("visit_id")]
        claims = await fin_repo.patient_claims(visit_ids) if visit_ids else []
    else:
        claims = await fin_repo.all_claims()

    high_value_denied = [
        c for c in claims
        if str(c.get("status", "")).lower() in ("denied", "rejected")
        and float(c.get("claim_amount") or 0) > 100_000
    ]
    payer_denials: dict[str, int] = {}
    for c in claims:
        if str(c.get("status", "")).lower() in ("denied", "rejected"):
            tpa = c.get("tpa_id", "unknown")
            payer_denials[tpa] = payer_denials.get(tpa, 0) + 1
    repeat_offenders = {tpa: cnt for tpa, cnt in payer_denials.items() if cnt >= 2}

    analysis = await _exec(
        task_id="exec__escalation_recommendations_rev",
        description=(
            "Generate revenue escalation recommendations. "
            "For each high-value denied claim (amount > 100000): add escalation with priority 1, target = tpa_id. "
            "For each repeat-offender payer (denial_count >= 2): add escalation with priority 2. "
            "escalation_count = len(escalations). "
            "escalations is list of {priority (int), action (str), target (str), reason (str)}."
        ),
        input_schema={
            "high_value_denied_count": "int — denied claims with amount > 100000",
            "repeat_offenders": "dict — {tpa_id: denial_count} for payers with >= 2 denials",
        },
        output_fields=["escalation_count", "escalations"],
        input_data={
            "high_value_denied_count": len(high_value_denied),
            "repeat_offenders": repeat_offenders,
        },
    )

    result = {
        "escalated":        analysis.get("escalation_count", 0) > 0,
        "escalation_count": analysis.get("escalation_count", 0),
        "escalations":      analysis.get("escalations", []),
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_rev_denial_prevention",
        "result": {"escalation_count": result["escalation_count"]},
    })
    logger.info(
        "escalation_recommendations_rev  session=%s  escalations=%d",
        inp.session_id, result["escalation_count"],
    )
    return result


# NOTE (revenue/billing split 2026-06): get_patient_billing, _resolved_patients
# and create_billing_request moved to agents/billing/activities.py -- billing_agent
# now owns single-patient invoice lookup and bill-generation execution. Commented
# out rather than deleted -- remove once stable.
# -- sa_rev_patient_billing ----------------------------------------------------
#
# @activity.defn
# async def get_patient_billing(inp: PatientBillingInput) -> dict:
#     """
#     Extract a patient token from the goal text, then fetch all invoices
#     and claims for that patient from Hasura.
#     """
#     await broadcast(inp.session_id, {
#         "type": "sub_agent_started",
#         "sub_agent": "sa_rev_patient_billing",
#     })
#
#     match = re.search(r'\b([A-Z]{2,}-?\d{4,}|\d{6,})\b', inp.goal)
#     patient_token = match.group(1) if match else "UNKNOWN"
#
#     if not patient_token or patient_token == "UNKNOWN":
#         result = {
#             "patient_token": None,
#             "error": "Could not identify patient from query -- please include the patient token or ID",
#         }
#         await broadcast(inp.session_id, {
#             "type": "sub_agent_completed",
#             "sub_agent": "sa_rev_patient_billing",
#             "result": result,
#         })
#         return result
#
#     invoices = await fin_repo.patient_invoices(patient_token)
#
#     visit_ids = [inv["visit_id"] for inv in invoices if inv.get("visit_id")]
#     claims    = await fin_repo.patient_claims(visit_ids)
#
#     total_billed = sum(float(inv.get("grand_total")  or 0) for inv in invoices)
#     total_paid   = sum(float(inv.get("paid_amount")  or 0) for inv in invoices)
#     outstanding  = round(total_billed - total_paid, 2)
#
#     unpaid_invoices = [i for i in invoices if i.get("payment_status") in ("Unpaid", "Partial")]
#     pending_claims  = [c for c in claims if str(c.get("status", "")).lower() == "pending"]
#     denied_claims   = [c for c in claims if str(c.get("status", "")).lower() in ("denied", "rejected")]
#
#     if denied_claims:
#         denied_amt = sum(float(c.get("claim_amount") or 0) for c in denied_claims)
#         await broadcast(inp.session_id, {
#             "type": "alert",
#             "severity": "warning",
#             "message": (
#                 f"Patient {patient_token}: {len(denied_claims)} denied claim(s) -- "
#                 f"₹{denied_amt:,.0f} at risk. Resubmission required."
#             ),
#         })
#
#     result = {
#         "patient_token":  patient_token,
#         "invoice_count":  len(invoices),
#         "total_billed":   round(total_billed, 2),
#         "total_paid":     round(total_paid, 2),
#         "outstanding":    outstanding,
#         "unpaid_count":   len(unpaid_invoices),
#         "claims_count":   len(claims),
#         "claims_pending": len(pending_claims),
#         "claims_denied":  len(denied_claims),
#         "invoices":       invoices[:10],
#     }
#
#     await broadcast(inp.session_id, {
#         "type": "sub_agent_completed",
#         "sub_agent": "sa_rev_patient_billing",
#         "result": result,
#     })
#     logger.info(
#         "patient billing  session=%s  patient=%s  invoices=%d  outstanding=%.2f",
#         inp.session_id, patient_token, len(invoices), outstanding,
#     )
#     return result
#
#
# -- sa_rev_initiate_billing ---------------------------------------------------
#
# def _resolved_patients(raw, goal: str) -> list[dict]:
#     """Normalise the ``session_patient:{sid}`` cache into a list of patient contexts.
#
#     Falls back to a single token parsed from the goal text when no patient was
#     resolved by patient verification (same regex the lookup task uses)."""
#     if isinstance(raw, list):
#         patients = [p for p in raw if isinstance(p, dict)]
#     elif isinstance(raw, dict):
#         patients = [raw]
#     else:
#         patients = []
#     if patients:
#         return patients
#     match = re.search(r'\b([A-Z]{2,}-?\d{4,}|\d{6,})\b', goal or "")
#     if match:
#         tok = match.group(1)
#         return [{"token": tok, "patient_token": tok}]
#     return []
#
#
# @activity.defn
# async def create_billing_request(inp: InitiateBillingInput) -> dict:
#     """Create a bill-generation request for the resolved patient(s).
#
#     This does NOT write to the HIS directly. It STAGES the request(s) in Redis --
#     exactly like discharge/appointment staging -- so nothing is billed until the
#     user commits. When the session is committed (the recommendation is pushed to
#     the HIS), ``commit_session`` inserts these into ``hospilot.billing_requests``
#     for the DB side to turn into actual bills.
#
#     We never fabricate amounts or line items here: the request carries only patient
#     and encounter references plus ``generate_from_charges``; the DB side assembles
#     the bill from the patient's recorded charges.
#     """
#     await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_rev_initiate_billing"})
#
#     raw = await cache.get(f"session_patient:{inp.session_id}")
#     patients = _resolved_patients(raw, inp.goal)
#
#     requests: list[dict] = []
#     for p in patients:
#         token = p.get("patient_token") or p.get("token")
#         if not token:
#             continue
#         requests.append({
#             "patient_token":         token,
#             "patient_name":          p.get("patient_name"),
#             "uhid":                  p.get("uhid"),
#             "visit_id":              p.get("current_visit_id") or p.get("visit_id"),
#             "admission_id":          p.get("admission_id"),
#             "invoice_type":          p.get("invoice_type") or "IPD",
#             "generate_from_charges": True,
#             "source":                "initiate_billing",
#         })
#
#     if not requests:
#         result = {
#             "status":           "no_patient",
#             "patient_count":    0,
#             "billing_requests": [],
#             "error": "No resolved patient to bill -- run patient verification or include a patient token in the goal.",
#         }
#         await broadcast(inp.session_id, {
#             "type": "sub_agent_completed", "sub_agent": "sa_rev_initiate_billing", "result": result,
#         })
#         logger.info("create_billing_request  session=%s  no resolved patient", inp.session_id)
#         return result
#
#     # Stage for the commit step (HIS push). Mirrors discharge/appointment staging.
#     await cache.stage(inp.session_id, "billing", requests)
#
#     await hasura.write_audit(
#         session_id=inp.session_id,
#         agent_id="billing_agent",
#         event_type="billing_request_staged",
#         payload={"patient_count": len(requests), "tokens": [r["patient_token"] for r in requests]},
#     )
#
#     result = {
#         "status":           "staged",
#         "patient_count":    len(requests),
#         "patients_billed":  [r["patient_token"] for r in requests],
#         "billing_requests": requests,
#     }
#     await broadcast(inp.session_id, {
#         "type":          "billing_request_created",
#         "sub_agent":     "sa_rev_initiate_billing",
#         "patient_count": len(requests),
#         "patients":      [r["patient_token"] for r in requests],
#         "status":        "staged",
#     })
#     await broadcast(inp.session_id, {
#         "type": "sub_agent_completed", "sub_agent": "sa_rev_initiate_billing",
#         "result": {"patient_count": len(requests), "status": "staged"},
#     })
#     logger.info("create_billing_request  session=%s  patients=%d  staged", inp.session_id, len(requests))
#     return result
