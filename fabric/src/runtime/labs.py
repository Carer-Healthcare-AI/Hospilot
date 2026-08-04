"""Lab orders, results, and the lab-operations tables.

Two upstreams behind one prefix (see service/lab.py):
  • FHIR, streamed — orders (ServiceRequest), results (Observation), samples
    (Specimen), analyzers (Device)
  • plain REST, never cached — qc logs, reflex/validation rules, capacity,
    critical escalations
"""

from fastapi import APIRouter, Query

from service import clinical, lab as lab_svc

router = APIRouter()


# NOTE: /labs/orders and /labs/orders/pending are equivalent aliases —
# clinical.lab_orders() already queries only the pending statuses (active, on-hold).
# Both are kept because both are called; neither returns completed orders.
@router.get("/labs/orders", summary="Pending lab orders (active + on-hold)")
async def lab_orders_all():
    return await clinical.lab_orders()


@router.get("/labs/orders/pending", summary="Pending lab orders — alias of /labs/orders")
async def lab_orders_pending():
    return await clinical.lab_orders()


@router.get("/labs/results", summary="Lab results, optionally filtered by patient token and/or test code")
async def lab_results(patient: str | None = Query(None), test_code: str | None = Query(None)):
    return await clinical.lab_results(patient_token=patient, test_code=test_code)


@router.get("/labs/samples", summary="Lab specimens with collection and status")
async def lab_samples():
    return await lab_svc.samples()


@router.get("/labs/analyzers", summary="Lab analyzer devices and their state")
async def lab_analyzers():
    return await lab_svc.analyzers()


@router.get("/labs/qc-logs", summary="Quality-control runs over the last N hours")
async def lab_qc_logs(hours: int = Query(24)):
    return await lab_svc.qc_logs(hours=hours)


@router.get("/labs/reflex-rules", summary="Reflex-testing rules (auto follow-up tests)")
async def lab_reflex_rules():
    return await lab_svc.reflex_rules()


@router.get("/labs/validation-rules", summary="Result validation / auto-verification rules")
async def lab_validation_rules():
    return await lab_svc.validation_rules()


@router.get("/labs/capacity", summary="Daily lab throughput history over the last N days")
async def lab_capacity(days: int = Query(30)):
    return await lab_svc.capacity_history(days=days)


@router.get("/labs/critical-escalations", summary="Critical results and their escalation state")
async def lab_critical_escalations():
    return await lab_svc.critical_escalations()
