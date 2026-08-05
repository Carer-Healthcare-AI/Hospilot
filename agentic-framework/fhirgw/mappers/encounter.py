"""admission/visit <-> FHIR Encounter.

IPD admissions  -> Encounter.class = IMP
ER visits       -> Encounter.class = EMER

Timestamps and the raw internal status are preserved in extensions (FHIR
dateTime normalizes/rejects some values and the status mapping isn't 1:1), so
to_internal reproduces the exact projection agents consume.
"""

from fhir.resources.encounter import Encounter

from fhirgw import terminology as T, extensions as X, identifiers as ID, narrative as N
from fhirgw.mappers._common import parse_dt_safe, ref_id, location_ref_id, reason_text

ADMISSION_KEYS = ("id", "patient_token", "bed_id", "admitted_at", "expected_discharge_at", "status")
VISIT_KEYS = ("id", "patient_token", "department_id", "arrived_at", "status", "chief_complaint")


# --- Admission (IMP) ----------------------------------------------------------
def admission_to_fhir(adm: dict) -> Encounter:
    exts: list[dict] = []
    if adm.get("admitted_at") is not None:
        exts.append(X.ext_string(X.EXT_ADM_ADMITTED_AT, adm["admitted_at"]))
    if adm.get("expected_discharge_at") is not None:
        exts.append(X.ext_string(X.EXT_ADM_EXPECTED_DISCHARGE, adm["expected_discharge_at"]))
    if adm.get("status") is not None:
        exts.append(X.ext_string(X.EXT_ADM_RAW_STATUS, adm["status"]))

    fhir_status = T.encounter_status_to_fhir(adm.get("status"))
    kwargs: dict = {
        "id": str(adm["id"]),
        "identifier": [ID.identifier(ID.sys_admission(), adm["id"])],
        "status": fhir_status,
        "class": [{"coding": [T.ENCOUNTER_CLASS_IMP]}],
        "text": N.text(f"Inpatient admission {adm['id']} -- {fhir_status}"),
    }
    if adm.get("patient_token"):
        kwargs["subject"] = ID.patient_reference(adm["patient_token"])
    if adm.get("bed_id"):
        kwargs["location"] = [{"location": ID.reference("Location", adm["bed_id"])}]
    start = parse_dt_safe(adm.get("admitted_at"))
    if start:
        kwargs["actualPeriod"] = {"start": start}
    if exts:
        kwargs["extension"] = exts
    return Encounter(**kwargs)


def admission_to_internal(enc: Encounter) -> dict:
    return {
        "id": enc.id,
        "patient_token": ref_id(enc.subject) or "",
        "bed_id": location_ref_id(enc.location),
        "admitted_at": X.get_ext(enc.extension, X.EXT_ADM_ADMITTED_AT),
        "expected_discharge_at": X.get_ext(enc.extension, X.EXT_ADM_EXPECTED_DISCHARGE),
        "status": X.get_ext(enc.extension, X.EXT_ADM_RAW_STATUS),
    }


# --- ER visit (EMER) ----------------------------------------------------------
def visit_to_fhir(visit: dict) -> Encounter:
    exts: list[dict] = []
    if visit.get("arrived_at") is not None:
        exts.append(X.ext_string(X.EXT_VISIT_ARRIVED_AT, visit["arrived_at"]))
    if visit.get("status") is not None:
        exts.append(X.ext_string(X.EXT_VISIT_RAW_STATUS, visit["status"]))
    if visit.get("triage_score") is not None:
        exts.append(X.ext_int(X.EXT_VISIT_TRIAGE_SCORE, visit["triage_score"]))

    fhir_status = T.encounter_status_to_fhir(visit.get("status"))
    kwargs: dict = {
        "id": str(visit["id"]),
        "identifier": [ID.identifier(ID.sys_visit(), visit["id"])],
        "status": fhir_status,
        "class": [{"coding": [T.ENCOUNTER_CLASS_EMER]}],
        "text": N.text(f"Emergency visit {visit['id']} -- {fhir_status}"),
    }
    if visit.get("patient_token"):
        kwargs["subject"] = ID.patient_reference(visit["patient_token"])
    if visit.get("department_id"):
        kwargs["serviceProvider"] = ID.reference("Organization", visit["department_id"])
    if visit.get("chief_complaint"):
        # R5: Encounter.reason[].value is a CodeableReference (concept | reference).
        kwargs["reason"] = [{"value": [{"concept": {"text": str(visit["chief_complaint"])}}]}]
    start = parse_dt_safe(visit.get("arrived_at"))
    if start:
        kwargs["actualPeriod"] = {"start": start}
    ts = visit.get("triage_score")
    if ts in T.TRIAGE_PRIORITY:
        code, disp = T.TRIAGE_PRIORITY[ts]
        kwargs["priority"] = {"coding": [{"system": T.SYS_ACT_PRIORITY, "code": code, "display": disp}]}
    if exts:
        kwargs["extension"] = exts
    return Encounter(**kwargs)


def visit_to_internal(enc: Encounter) -> dict:
    return {
        "id": enc.id,
        "patient_token": ref_id(enc.subject) or "",
        "department_id": ref_id(enc.serviceProvider),
        "arrived_at": X.get_ext(enc.extension, X.EXT_VISIT_ARRIVED_AT),
        "status": X.get_ext(enc.extension, X.EXT_VISIT_RAW_STATUS),
        "chief_complaint": reason_text(enc.reason),
    }


# --- dispatcher (by identifier system; robust to the `class` keyword field) ---
def to_internal(enc: Encounter) -> dict:
    systems = []
    for i in (enc.identifier or []):
        s = i.get("system") if isinstance(i, dict) else getattr(i, "system", None)
        if s:
            systems.append(s)
    if ID.sys_visit() in systems:
        return visit_to_internal(enc)
    return admission_to_internal(enc)
