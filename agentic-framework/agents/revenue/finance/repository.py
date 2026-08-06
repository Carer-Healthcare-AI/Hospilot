"""Financial data-access layer for the revenue & billing agents.

Two backends, selected at call time (mirrors `fhirgw.repository`):

  * **External financial API (CarerOS)** -- when `settings.financial_api_base_url`
    is set, fetch invoices/claims/etc. from CarerOS's plain-REST `…/api/financial`
    API (`finance.client`). Hospilot treats CarerOS as the external billing source.
  * **Local Hasura/Redis projection** (fallback) -- the existing `cache.*` / `hasura.*`
    calls, unchanged.

The CarerOS API returns the *same field names* the agents already read
(`grand_total`, `payment_status`, `claim_amount`, `tpa_id`, `visit_id`, …), so
this is a drop-in source swap -- the agents' dict logic is untouched.
"""

from cache import redis as cache
from db.hasura import hasura
from agents.revenue.finance import client


# --- Invoices ------------------------------------------------------------------
async def all_invoices() -> list[dict]:
    """All invoices (agents filter by payment_status/type locally)."""
    if client.configured():
        return await client.invoices()
    # legacy: cache holds all invoices; Hasura fallback returns outstanding only
    return await cache.get_all_invoices() or await hasura.get_outstanding_invoices()


async def patient_invoices(patient_token: str) -> list[dict]:
    """All invoices for one patient (by UHID/token)."""
    if client.configured():
        return await client.invoices(patient=patient_token)
    return await hasura.get_patient_invoices(patient_token)


# --- Claims ------------------------------------------------------------------
async def all_claims() -> list[dict]:
    """All claims (agents filter by status/tpa/amount locally)."""
    if client.configured():
        return await client.claims()
    return await hasura.carerOS_get_claims()


async def patient_claims(visit_ids: list[str]) -> list[dict]:
    """Claims for a set of visit ids (patient billing)."""
    if not visit_ids:
        return []
    if client.configured():
        return await client.claims(visit_id=",".join(str(v) for v in visit_ids))
    return await hasura.get_patient_claims(visit_ids)
