"""Appointments and doctor slots — filtered lookups plus booking.

`appointment` and `doctor_slot` are streamed, so the backend caches current state.
These routes stay because agents need filtered queries (by patient, provider,
department, status, date, specialization) that a per-record lookup can't answer.

Writes queue a PendingChange rather than calling the DB directly — see
service/appointments.py.
"""

from typing import Optional

from fastapi import APIRouter, Query

from service import appointments as appt_svc

router = APIRouter()


@router.get("/appointments", summary="List appointments with optional filters")
async def get_appointments(
    patient_id: Optional[str] = Query(None, description="Filter by patient ID"),
    provider_id: Optional[str] = Query(None, description="Filter by provider/doctor ID"),
    department_id: Optional[str] = Query(None, description="Filter by department ID"),
    status: Optional[str] = Query(None, description="Filter by appointment status"),
    date: Optional[str] = Query(None, description="Filter by appointment date (YYYY-MM-DD)"),
):
    return await appt_svc.list_all(
        patient_id=patient_id,
        provider_id=provider_id,
        department_id=department_id,
        status=status,
        date=date,
    )


@router.post("/appointments", summary="Create a new appointment", status_code=201)
async def create_appointment(body: dict):
    return await appt_svc.create(body)


@router.get("/appointments/slots", summary="List available appointment slots")
async def get_appointment_slots(
    provider_id: Optional[str] = Query(None, description="Filter by provider ID"),
    date: Optional[str] = Query(None, description="Filter by slot date (YYYY-MM-DD)"),
    specialization: Optional[str] = Query(None, description="Filter by specialization"),
):
    return await appt_svc.slots(
        provider_id=provider_id,
        date=date,
        specialization=specialization,
    )


@router.patch(
    "/appointments/slots/{slot_id}/book",
    summary="Book an appointment slot (mark as booked)",
)
async def book_slot(slot_id: str):
    return await appt_svc.book_slot(slot_id)
