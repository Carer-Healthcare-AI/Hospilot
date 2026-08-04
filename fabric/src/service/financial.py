"""Financial reads — thin wrappers over the DB's plain-REST financial API.

These responses are already the dict shapes the revenue/billing agents want, so
Fabric passes them straight through (no FHIR involved). See CarerOS-Financial-API-Spec.
Uses the generic plain-REST client (clients.rest_client) bound to the financial base URL.

Delivery paths (see fabric/README.md for the full table):
  • streamed → Kafka → the backend's internal DB:  invoices, claims
      Registered in topic_map.REST_ENTITIES as `invoice` / `claim` — the two the agents
      need to react to. They keep HTTP routes as well, for filtered/list queries.
  • runtime pass-through (the other 11):  line items, claim history/queries, payments,
      payment entries, refunds, contracts, contract rates, collections, reconciliation.
      None are cached — every read hits the DB live.
"""

import logging

from clients import rest_client as rc
from config import settings

logger = logging.getLogger("financial")


def _base() -> str:
    return settings.financial_api_base_url


async def invoices(payment_status=None, patient=None, invoice_type=None, limit=None, offset=None) -> list[dict]:
    return await rc.list_(
        _base(), "invoices",
        payment_status=payment_status, patient=patient, invoice_type=invoice_type,
        limit=limit, offset=offset,
    )


async def invoice_line_items(invoice_id: str) -> list[dict]:
    return await rc.safe_list(_base(), f"invoices/{invoice_id}/line_items")


async def claims(status=None, patient=None, visit_id=None, limit=None, offset=None) -> list[dict]:
    return await rc.list_(
        _base(), "claims",
        status=status, patient=patient, visit_id=visit_id, limit=limit, offset=offset,
    )


async def claim_line_items(claim_id: str) -> list[dict]:
    return await rc.list_(_base(), f"claims/{claim_id}/line_items")


async def claim_history(claim_id: str) -> list[dict]:
    return await rc.list_(_base(), f"claims/{claim_id}/history")


async def claim_queries(claim_id: str) -> list[dict]:
    return await rc.list_(_base(), f"claims/{claim_id}/queries")


async def payments() -> list[dict]:
    return await rc.list_(_base(), "payments")


async def payment_entries(payment_id: str) -> list[dict]:
    return await rc.safe_list(_base(), f"payments/{payment_id}/entries")


async def refunds(invoice_id=None) -> list[dict]:
    # Upstream nests refunds under payments: /api/financial/payments/refunds.
    # A bare /refunds 404s, and safe_list turns that into [] — i.e. refunds silently
    # look empty rather than erroring. Keep the path in step with the DB's spec.
    return await rc.safe_list(_base(), "payments/refunds", invoice_id=invoice_id)


async def contracts() -> list[dict]:
    return await rc.list_(_base(), "contracts")


async def contract_rates(contract_id: str) -> list[dict]:
    return await rc.list_(_base(), f"contracts/{contract_id}/rates")


async def collections(date: str) -> dict | None:
    # tolerant: collections is empty/optional in the DB today
    try:
        return await rc.get_one(_base(), f"collections/{date}")
    except Exception:
        return None


async def reconciliation(date: str) -> dict | None:
    try:
        return await rc.get_one(_base(), f"reconciliation/{date}")
    except Exception:
        return None
