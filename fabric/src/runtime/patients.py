"""⚠ PHI — the only Fabric routes that return patient-identifying data.

Everything else Fabric serves is pseudonymous: records reference an opaque
`patient_token` and nothing more. The routes here resolve that token to real
demographics (name, mobile, UHID) by reading the DB's FHIR Patient, so treat this
module as the PHI boundary:

  • never deploy with FABRIC_API_KEY unset where these are reachable
  • don't log responses
  • /patients/by-mobile is a reverse lookup — an unauthenticated caller could
    enumerate patients by phone number

Hospilot itself stores no patient table; the demographics pass through from the HIS
and are not persisted here. See service/transform.py::patient().

Also holds patient registration: Fabric only queues a request for the hospital's
staff worklist — it never creates a patient upstream.

Static sub-paths are declared before `/{token}` so they aren't shadowed.
"""

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from runtime._common import _or_404
from service import clinical

logger = logging.getLogger("normalized")
router = APIRouter()


@router.get("/patients/tokens", summary="All known patient tokens (pseudonymous — no PHI)")
async def patient_tokens():
    return await clinical.patient_tokens()


@router.get("/patients", summary="⚠ PHI — resolve comma-separated tokens to demographics")
async def patients(ids: str = Query(..., description="comma-separated patient tokens")):
    """{token: {first_name, last_name, uhid, ...}} — replaces db.hasura.get_patient_names."""
    toks = [t.strip() for t in ids.split(",") if t.strip()]
    return await clinical.patient_names(toks)


@router.get("/patients/by-mobile", summary="⚠ PHI — reverse-lookup a patient by phone number")
async def patient_by_mobile(mobile: str = Query(..., description="Phone number — any format; normalised to last 10 digits")):
    return await clinical.patient_by_mobile(mobile)


@router.get("/patients/{token}", summary="⚠ PHI — one patient's demographics by token")
async def patient(token: str):
    return await _or_404(await clinical.patient(token), f"Patient {token}")


# ─── patient registration ─────────────────────────────────────────────────────
# In-memory store for pending registration requests (TTL handled by backend's
# 24h reaper; process restart is safe — backend re-sends on timeout).
_pending_registrations: dict[str, dict] = {}


class _RegisterRequest(BaseModel):
    mobile: str
    name_hint: str | None = None
    session_id: str
    source: str = "patient_verification_agent"


@router.post("/patients/register", status_code=202,
             summary="Queue a patient-registration request for the hospital's staff worklist")
async def register_patient(body: _RegisterRequest):
    """Receive a registration request from the backend, store it as pending, and
    return immediately. The actual patient creation is manual (DB side worklist).
    The diff poller detects the new patient and publishes hospilot.data.patient."""
    digits = re.sub(r"\D", "", body.mobile)[-10:]
    request_id = f"reg_{uuid.uuid4().hex[:8]}"
    _pending_registrations[request_id] = {
        "request_id": request_id,
        "mobile": digits,
        "mobile_display": body.mobile,
        "name_hint": body.name_hint,
        "session_id": body.session_id,
        "source": body.source,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("patient registration pending  mobile=%s  session=%s  req=%s",
                digits, body.session_id, request_id)
    return {"request_id": request_id, "status": "pending"}


@router.get("/patients/register/pending", summary="Registration requests awaiting staff action")
async def pending_registrations():
    """Pending patient registration requests — polled by the DB-side staff worklist."""
    return list(_pending_registrations.values())


@router.delete("/patients/register/{request_id}", summary="Mark a registration request complete")
async def complete_registration(request_id: str):
    """Mark a registration request complete (called when staff confirm patient created)."""
    entry = _pending_registrations.pop(request_id, None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Registration request not found")
    logger.info("patient registration completed  req=%s", request_id)
    return {"request_id": request_id, "status": "completed"}
