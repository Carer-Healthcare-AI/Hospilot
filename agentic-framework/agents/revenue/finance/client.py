"""HTTP client for the financial API.

When `settings.fabric_base_url` is set, calls Fabric's `/financial` endpoints
(Fabric proxies the DB's plain-REST financial API). Falls back to
`settings.financial_api_base_url` for legacy deployments.

List endpoints return either `{"data": [...]}` or plain `[...]`; both are
normalised by `_list()`.
"""

import logging

import httpx

from config import settings

logger = logging.getLogger("financial_client")

_DEFAULT_LIMIT = 200


def configured() -> bool:
    return bool(settings.fabric_base_url or settings.financial_api_base_url)


def _base_url() -> str:
    if settings.fabric_base_url:
        return settings.fabric_base_url.rstrip("/") + "/financial"
    return settings.financial_api_base_url.rstrip("/")


def _headers() -> dict:
    h = {"Accept": "application/json"}
    if settings.fabric_base_url:
        key = settings.fabric_api_key
    else:
        key = settings.financial_api_key or settings.fhir_ehr_api_key
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _clean(params: dict | None) -> dict:
    return {k: v for k, v in (params or {}).items() if v not in (None, "", [])}


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_base_url()}/{path.lstrip('/')}", params=_clean(params), headers=_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def _list(path: str, params: dict | None = None) -> list[dict]:
    data = await _get(path, {"limit": _DEFAULT_LIMIT, **(params or {})})
    if isinstance(data, dict):
        return data.get("data") or []
    return data or []


async def _object(path: str, params: dict | None = None) -> dict | None:
    data = await _get(path, params)
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            return data["data"] or None
        return data or None
    return None


# --- Invoices ------------------------------------------------------------------
async def invoices(
    payment_status: str | None = None,
    patient: str | None = None,
    invoice_type: str | None = None,
) -> list[dict]:
    return await _list("invoices", {
        "payment_status": payment_status, "patient": patient, "invoice_type": invoice_type,
    })


async def invoice_line_items(invoice_id: str) -> list[dict]:
    # Fabric's own route is hyphenated; the DB's underlying financial API (used
    # directly by the legacy financial_api_base_url fallback) is underscored.
    seg = "line-items" if settings.fabric_base_url else "line_items"
    return await _list(f"invoices/{invoice_id}/{seg}")


# --- Claims ------------------------------------------------------------------
async def claims(
    status: str | None = None,
    patient: str | None = None,
    visit_id: str | None = None,
) -> list[dict]:
    return await _list("claims", {
        "status": status, "patient": patient, "visit_id": visit_id,
    })


async def claim_line_items(claim_id: str) -> list[dict]:
    # Same Fabric-vs-DB path-spelling split as invoice_line_items above.
    seg = "line-items" if settings.fabric_base_url else "line_items"
    return await _list(f"claims/{claim_id}/{seg}")


async def claim_history(claim_id: str) -> list[dict]:
    return await _list(f"claims/{claim_id}/history")


async def claim_queries(claim_id: str) -> list[dict]:
    return await _list(f"claims/{claim_id}/queries")


# --- Payments / refunds ---------------------------------------------------------
async def payments() -> list[dict]:
    return await _list("payments")


async def payment_entries(payment_id: str) -> list[dict]:
    return await _list(f"payments/{payment_id}/entries")


async def refunds(invoice_id: str | None = None) -> list[dict]:
    return await _list("refunds", {"invoice_id": invoice_id})


# --- Contracts ------------------------------------------------------------------
async def contracts() -> list[dict]:
    return await _list("contracts")


async def contract_rates(contract_id: str) -> list[dict]:
    return await _list(f"contracts/{contract_id}/rates")


# --- Daily collections / reconciliation (single object per date) -----------------
async def collections(date: str) -> dict | None:
    return await _object(f"collections/{date}")


async def reconciliation(date: str) -> dict | None:
    return await _object(f"reconciliation/{date}")
