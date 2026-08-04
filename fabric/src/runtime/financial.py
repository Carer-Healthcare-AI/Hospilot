"""Revenue-cycle reads — invoices, claims, payments, contracts, reconciliation.

Passed through from the DB's plain-REST financial API with no FHIR involved (see
service/financial.py). Of these, only invoices and claims are streamed to the
backend; the other eleven are read live on every call.
"""

from fastapi import APIRouter

from service import financial

router = APIRouter()


@router.get("/financial/invoices", summary="Invoices, filterable by payment status, patient and type")
async def fin_invoices(
    payment_status: str | None = None,
    patient: str | None = None,
    invoice_type: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    return await financial.invoices(
        payment_status=payment_status, patient=patient, invoice_type=invoice_type,
        limit=limit, offset=offset,
    )


@router.get("/financial/invoices/{invoice_id}/line-items", summary="Line items for one invoice")
async def fin_invoice_line_items(invoice_id: str):
    return await financial.invoice_line_items(invoice_id)


@router.get("/financial/claims", summary="Insurance claims, filterable by status, patient and visit")
async def fin_claims(
    status: str | None = None,
    patient: str | None = None,
    visit_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    return await financial.claims(
        status=status, patient=patient, visit_id=visit_id, limit=limit, offset=offset,
    )


@router.get("/financial/claims/{claim_id}/line-items", summary="Line items for one claim")
async def fin_claim_line_items(claim_id: str):
    return await financial.claim_line_items(claim_id)


@router.get("/financial/claims/{claim_id}/history", summary="Status-change history for one claim")
async def fin_claim_history(claim_id: str):
    return await financial.claim_history(claim_id)


@router.get("/financial/claims/{claim_id}/queries", summary="Payer queries raised against one claim")
async def fin_claim_queries(claim_id: str):
    return await financial.claim_queries(claim_id)


@router.get("/financial/payments", summary="Payments received")
async def fin_payments():
    return await financial.payments()


@router.get("/financial/payments/{payment_id}/entries", summary="Allocation entries for one payment")
async def fin_payment_entries(payment_id: str):
    return await financial.payment_entries(payment_id)


@router.get("/financial/refunds", summary="Refunds, optionally scoped to one invoice")
async def fin_refunds(invoice_id: str | None = None):
    return await financial.refunds(invoice_id=invoice_id)


@router.get("/financial/contracts", summary="Payer contracts")
async def fin_contracts():
    return await financial.contracts()


@router.get("/financial/contracts/{contract_id}/rates", summary="Negotiated rates under one contract")
async def fin_contract_rates(contract_id: str):
    return await financial.contract_rates(contract_id)


@router.get("/financial/collections/{date}", summary="Collections total for one date (YYYY-MM-DD)")
async def fin_collections(date: str):
    return await financial.collections(date)


@router.get("/financial/reconciliation/{date}", summary="Payment reconciliation for one date (YYYY-MM-DD)")
async def fin_reconciliation(date: str):
    return await financial.reconciliation(date)
