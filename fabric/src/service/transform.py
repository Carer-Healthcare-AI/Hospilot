"""Standard FHIR R5 → normalized dict.

The DB's FHIR is **standard, extension-free** (per the FHIR spec), so we read the
data from standard elements. We still reuse the mappers' `to_internal` (which
already pulls references/values from standard elements) and then fill the few
fields the mappers sourced from Hospilot extensions (admitted_at, status,
recorded_at, is_critical, ward, …) from their standard-element equivalents —
preferring a Hospilot extension when the DB happens to include one (lossless).

Output shapes match exactly what the main backend's db.hasura methods return.
"""

from fhirgw import terminology as T, extensions as X
from fhirgw.mappers import location as loc_map, encounter as enc_map, observation as obs_map
from fhirgw.mappers._common import ref_id, bare_id, coding_code

# reverse of terminology.LOCATION_OPER_STATUS (occupancy code → bed status string)
_OPER_TO_STATUS = {"U": "Available", "O": "Occupied", "K": "Dirty", "H": "Cleaning"}
_EXT_ADM_TRANSFER_PENDING = X.EXT_BASE + "admission-transfer-pending"
# reverse of terminology.INTERPRETATION_MAP (interpretation code → hospilot flag)
_INTERP_TO_FLAG = {c: flag for flag, (c, _d) in T.INTERPRETATION_MAP.items()}


def _dt(value) -> str | None:
    """ISO-8601 string. fhir.resources parses FHIR dateTime to a Python datetime;
    use isoformat() (keeps the 'T') rather than str() (which uses a space)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _period_start(enc):
    p = getattr(enc, "actualPeriod", None)
    return getattr(p, "start", None) if p else None


def _reverse_status(fhir_status: str | None) -> str | None:
    if not fhir_status:
        return None
    candidates = T.encounter_status_to_internal(fhir_status)
    return candidates[0] if candidates else fhir_status


# ─── beds ───────────────────────────────────────────────────────────────────────
def bed(loc, wards_by_id: dict | None = None) -> dict:
    d = loc_map.to_internal(loc)                       # bed_number(name), is_active, ext fields
    d["id"] = bare_id(d.get("id"))
    if not d.get("status"):                            # standard: operationalStatus → status
        op = getattr(loc, "operationalStatus", None)
        d["status"] = _OPER_TO_STATUS.get(getattr(op, "code", None)) if op else None
    if not d.get("ward"):                              # standard: partOf → ward
        wid = ref_id(getattr(loc, "partOf", None))
        d["ward"] = (wards_by_id or {}).get(wid, wid)
    return d


def is_icu_bed(bed_dict: dict) -> bool:
    return "icu" in (bed_dict.get("ward") or "").lower()


# ─── admissions / visits ─────────────────────────────────────────────────────────
def admission(enc) -> dict:
    d = enc_map.admission_to_internal(enc)            # patient_token, bed_id (standard)
    d["id"] = bare_id(d.get("id"))
    d["patient_token"] = bare_id(d.get("patient_token")) or ""
    d["bed_id"] = bare_id(d.get("bed_id")) if d.get("bed_id") else None
    if not d.get("admitted_at"):
        d["admitted_at"] = _dt(_period_start(enc))
    if not d.get("status"):
        d["status"] = _reverse_status(enc.status)
    # Hospilot enrichment — present only if the DB exposes these extensions
    d["discharge_ready"] = X.get_ext(enc.extension, X.EXT_ADM_DISCHARGE_READY)
    d["discharge_blocked_reason"] = X.get_ext(enc.extension, X.EXT_ADM_DISCHARGE_BLOCKED_REASON)
    d["transfer_pending"] = X.get_ext(enc.extension, _EXT_ADM_TRANSFER_PENDING)
    return d


def visit(enc) -> dict:
    d = enc_map.visit_to_internal(enc)                # patient_token, department_id, chief_complaint
    d["id"] = bare_id(d.get("id"))
    d["patient_token"] = bare_id(d.get("patient_token")) or ""
    d["department_id"] = bare_id(d.get("department_id")) if d.get("department_id") else None
    if not d.get("arrived_at"):
        d["arrived_at"] = _dt(_period_start(enc))
    if not d.get("status"):
        # EMER source statuses are in-progress/completed/cancelled (not the IMP
        # "admitted" set), so keep the FHIR status value rather than IMP-reverse.
        d["status"] = enc.status
    d["triage_score"] = X.get_ext(enc.extension, X.EXT_VISIT_TRIAGE_SCORE)
    return d


# ─── vitals ───────────────────────────────────────────────────────────────────────
def _has_critical(o) -> bool:
    for interp in (getattr(o, "interpretation", None) or []):
        if coding_code(interp) == T.CRITICAL_INTERP[0]:
            return True
    return False


def vital(obs_list) -> dict:
    obs_list = list(obs_list)
    d = obs_map.vitals_to_internal(obs_list)          # measures + patient_token (standard)
    if not d:
        return d
    d["id"] = bare_id(d.get("id"))
    d["patient_token"] = bare_id(d.get("patient_token")) or ""
    if not d.get("recorded_at"):
        d["recorded_at"] = _dt(getattr(obs_list[0], "effectiveDateTime", None))
    if d.get("is_critical") is None:
        d["is_critical"] = any(_has_critical(o) for o in obs_list)
    return d


def group_vitals_by_reading(observations) -> dict[str, list]:
    """Group vital Observations by their shared reading uuid. The DB emits one
    Observation per measure with a measure-prefixed id (temp-/hr-/rr-/spo2-/bp-/gcs-
    + uuid); Hospilot's own mapper uses {uuid}.{loinc}. Stripping the measure prefix
    (bare_id) then the .loinc suffix yields the reading uuid in both cases."""
    groups: dict[str, list] = {}
    for o in observations:
        rid = bare_id(o.id or "").split(".")[0]
        groups.setdefault(rid, []).append(o)
    return groups


# ─── labs ───────────────────────────────────────────────────────────────────────
def _value(o):
    vq = getattr(o, "valueQuantity", None)
    if vq is not None and getattr(vq, "value", None) is not None:
        v = vq.value
        from decimal import Decimal
        return float(v) if isinstance(v, Decimal) else v
    return getattr(o, "valueString", None)


def lab_result(o) -> dict:
    vq = getattr(o, "valueQuantity", None)
    rr = getattr(o, "referenceRange", None)
    flag = None
    for interp in (getattr(o, "interpretation", None) or []):
        flag = _INTERP_TO_FLAG.get(coding_code(interp), flag)
    code = getattr(o, "code", None)
    return {
        "id": bare_id(o.id),
        "patient_token": bare_id(ref_id(getattr(o, "subject", None))) or "",
        "test_code": coding_code(code),
        "test_name": getattr(code, "text", None) if code else None,
        "result_value": _value(o),
        "unit": getattr(vq, "unit", None) if vq else None,
        "flag": flag,
        "reference_range": (getattr(rr[0], "text", None) if rr else None),
        "reported_at": _dt(getattr(o, "effectiveDateTime", None)),
    }


def _first(raw: dict, *keys):
    """First present, non-None value among `keys` (for tolerant raw-row mapping)."""
    for k in keys:
        if raw.get(k) is not None:
            return raw[k]
    return None


def lab_result_row(raw: dict) -> dict:
    """Normalize a RAW `hospilot.lab_results` row (from the /sync/lab_result API) into
    the SAME shape as `lab_result()` above, so polling mode emits the identical
    `lab_result` contract as the change_api feed.

    The sync API forwards raw DB columns whose exact names aren't part of Fabric's
    FHIR contract, so we map tolerantly across the likely candidates. If the live
    `schema.columns` differ, extend the candidate lists here (one place to fix).

    `flag` is the one column confirmed exact (CarerOS's `hospilot.lab_results.flag`,
    verbatim via `SELECT *`): its value domain is the word vocabulary
    Critical | High | Low | Normal — already matching `lab_result()`'s
    `_INTERP_TO_FLAG` output, so no code→word translation is needed here."""
    return {
        "id": str(_first(raw, "id") or ""),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "test_code": _first(raw, "test_code", "loinc_code", "code"),
        "test_name": _first(raw, "test_name", "name", "test"),
        "result_value": _first(raw, "result_value", "value", "result"),
        "unit": _first(raw, "unit", "units"),
        "flag": raw.get("flag"),
        "reference_range": _first(raw, "reference_range", "ref_range", "normal_range"),
        "reported_at": _dt(_first(raw, "reported_at", "resulted_at", "updated_at", "created_at")),
    }


# ─── raw-row mappers (Kafka change events carry the row; no re-read needed) ─────
# Each maps a RAW DB row from a `hospilot.changes.*` event onto the SAME normalized
# shape the FHIR path produces, so a cached entity never costs an extra HTTP read.
# Column names are matched tolerantly (the event forwards raw columns), so a schema
# that names things differently degrades to a re-read rather than to bad data.


def _as_bool(v):
    """Coerce a raw DB boolean-ish value; None stays None (field absent)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "t", "yes", "y", "1")


def _as_num(v):
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def _as_list(v):
    """Bed features arrive as a JSON array, a comma string, or a single value."""
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x is not None]
    if isinstance(v, str):
        txt = v.strip()
        if txt.startswith("[") and txt.endswith("]"):
            import json
            try:
                parsed = json.loads(txt)
                return [x for x in parsed if x is not None] if isinstance(parsed, list) else [txt]
            except ValueError:
                pass
        return [p.strip() for p in txt.split(",") if p.strip()]
    return [v]


def bed_row(raw: dict) -> dict:
    """Raw `hospilot.beds` row -> the `bed` contract.

    `status` is kept VERBATIM. The FHIR path collapses `reserved`/`vacating` onto
    `Occupied` (v2-0116 has no distinct code), losing a distinction the consumers
    actually test for — the raw row preserves it."""
    return {
        "id": str(_first(raw, "id") or ""),
        "bed_number": _first(raw, "bed_number", "bed_no", "number", "name"),
        "ward": _first(raw, "ward", "ward_name"),
        "room_type": _first(raw, "room_type", "roomtype"),
        "status": _first(raw, "status", "bed_status"),
        "is_active": _as_bool(_first(raw, "is_active", "active")),
        "branch_id": _first(raw, "branch_id", "branch"),
        "ventilation": _first(raw, "ventilation", "ventilator"),
        "room_sharing": _first(raw, "room_sharing", "sharing"),
        "proximity": _as_num(_first(raw, "proximity")),
        "floor": _as_num(_first(raw, "floor")),
        "wing": _first(raw, "wing"),
        "natural_light": _as_bool(_first(raw, "natural_light")),
        "noise_level": _first(raw, "noise_level"),
        "features": _as_list(_first(raw, "features", "feature")),
    }


def admission_row(raw: dict) -> dict:
    """Raw `hospilot.ipd_admissions` row -> the `admission` contract.

    `status` is the internal value verbatim (admitted / critical / improving / …),
    which the FHIR path can only recover from the admission-raw-status extension."""
    return {
        "id": str(_first(raw, "id") or ""),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "bed_id": _first(raw, "bed_id", "bed"),
        "admitted_at": _dt(_first(raw, "admitted_at", "admission_date", "created_at")),
        "expected_discharge_at": _dt(_first(raw, "expected_discharge_at", "expected_discharge")),
        "status": _first(raw, "status", "admission_status"),
        "discharge_ready": _as_bool(_first(raw, "discharge_ready")),
        "discharge_blocked_reason": _first(raw, "discharge_blocked_reason", "blocked_reason"),
        "transfer_pending": _as_bool(_first(raw, "transfer_pending")),
    }


def visit_row(raw: dict) -> dict:
    """Raw `hospilot.visits` row -> the `visit` contract."""
    return {
        "id": str(_first(raw, "id") or ""),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "department_id": _first(raw, "department_id", "department", "dept_id"),
        "arrived_at": _dt(_first(raw, "arrived_at", "arrival_time", "created_at")),
        "status": _first(raw, "status", "visit_status"),
        "chief_complaint": _first(raw, "chief_complaint", "complaint", "reason"),
        "triage_score": _as_num(_first(raw, "triage_score", "triage")),
    }


def lab_order_row(raw: dict) -> dict:
    """Raw `hospilot.lab_orders` row -> the `lab_order` contract.

    `test_name` lives on the lab_results relation in the FHIR projection, so a bare
    orders row may not carry it — the completeness guard in the consumer falls back
    to a re-read when it is missing."""
    return {
        "id": str(_first(raw, "id") or ""),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "status": _first(raw, "status", "order_status"),
        "priority": _first(raw, "priority"),
        "ordered_at": _dt(_first(raw, "ordered_at", "created_at")),
        "test_name": _first(raw, "test_name", "test", "name"),
    }


def nursing_task_row(raw: dict) -> dict:
    """Raw `hospilot.nursing_tasks` row -> the `task` contract.

    The raw table stores a `completed` boolean; the contract carries both that and a
    FHIR-style `status`, so the status is derived here the same way the projection does."""
    completed = _as_bool(_first(raw, "completed"))
    status = _first(raw, "status")
    if not status:
        status = "completed" if completed else "requested"
    return {
        "id": str(_first(raw, "id") or ""),
        "admission_id": _first(raw, "admission_id", "admission", "encounter_id"),
        "task": _first(raw, "task", "description", "task_name"),
        "due_at": _dt(_first(raw, "due_at", "scheduled_at", "due_date")),
        "assigned_to": _first(raw, "assigned_to", "owner", "assignee"),
        "completed": bool(completed),
        "status": status,
    }


def lab_sample_row(raw: dict) -> dict:
    """Raw `hospilot.lab_samples` row -> the `lab_sample` contract."""
    return {
        "id": str(_first(raw, "id") or ""),
        "order_id": _first(raw, "order_id", "lab_order_id"),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "barcode": _first(raw, "barcode", "accession_no", "accession"),
        "status": _first(raw, "status", "sample_status"),
        "type": _first(raw, "type", "sample_type", "specimen_type"),
        "collected_at": _dt(_first(raw, "collected_at", "collection_time")),
        "received_at": _dt(_first(raw, "received_at", "received_time")),
        "container_type": _first(raw, "container_type", "container", "tube_type"),
    }


def lab_analyzer_row(raw: dict) -> dict:
    """Raw `hospilot.lab_analyzers` row -> the `lab_analyzer` contract."""
    return {
        "id": str(_first(raw, "id") or ""),
        "name": _first(raw, "name", "analyzer_name", "device_name"),
        "model": _first(raw, "model", "model_number", "modelNumber"),
        "manufacturer": _first(raw, "manufacturer", "make", "vendor"),
        "status": _first(raw, "status", "analyzer_status"),
        "location_id": _first(raw, "location_id", "location", "lab_id"),
    }


def pharmacy_order_row(raw: dict) -> dict:
    """Raw `hospilot.pharmacy_orders` row -> the `pharmacy_order` contract.

    `medication` may be a foreign key to a drug master rather than a name; the
    completeness guard falls back to a re-read when it resolves empty."""
    return {
        "id": str(_first(raw, "id") or ""),
        "patient_token": str(_first(raw, "patient_token", "patient_id") or ""),
        "encounter_id": _first(raw, "encounter_id", "admission_id", "visit_id"),
        "prescriber_id": _first(raw, "prescriber_id", "prescriber", "doctor_id"),
        "medication": _first(raw, "medication", "medication_name", "drug_name", "drug"),
        "status": _first(raw, "status", "order_status"),
        "intent": _first(raw, "intent"),
        "priority": _first(raw, "priority"),
        "dosage": _first(raw, "dosage", "dosage_instruction", "sig"),
        "prescribed_at": _dt(_first(raw, "prescribed_at", "ordered_at", "created_at")),
    }


def pharmacy_inventory_row(raw: dict) -> dict:
    """Raw `hospilot.pharmacy_inventory` row -> the `pharmacy_inventory` contract."""
    return {
        "id": str(_first(raw, "id") or ""),
        "name": _first(raw, "name", "item_name", "drug_name"),
        "code": _first(raw, "code", "item_code", "sku"),
        "status": _first(raw, "status"),
        "category": _first(raw, "category", "item_category"),
        "qty_in_stock": _as_num(_first(raw, "qty_in_stock", "quantity", "stock_qty", "qty")),
        "unit": _first(raw, "unit", "uom", "units"),
        "expiry_date": _dt(_first(raw, "expiry_date", "expiry", "expires_at")),
    }


# ─── departments / patients ──────────────────────────────────────────────────────
def department(org) -> dict:
    typ = None
    types = getattr(org, "type", None)
    if types:
        typ = getattr(types[0], "text", None)
    return {"id": bare_id(org.id), "name": org.name, "type": typ}


def patient_token(pat) -> str | None:
    # The DB sets Patient.id = patient_token (= patients.id). identifier[] carries
    # UHID/ABHA, NOT the token — so the token is the resource id.
    return bare_id(pat.id)


def patient(pat) -> dict:
    """Full patient dict (demographics) — matches db.hasura.get_patient_names rows.

    ⚠ PHI — this is Fabric's ONLY function that returns patient-identifying data
    (name, mobile, UHID). It backs /patients, /patients/{token} and
    /patients/by-mobile; every other shape Fabric serves carries the opaque
    patient_token alone. Treat callers of this as the PHI boundary: never log the
    result, and keep FABRIC_API_KEY set wherever these routes are reachable.
    """
    names = getattr(pat, "name", None) or []
    name = names[0] if names else None
    given = " ".join(getattr(name, "given", None) or []) if name else None
    family = getattr(name, "family", None) if name else None
    idents = getattr(pat, "identifier", None) or []
    uhid = None
    for i in idents:
        if "uhid" in (getattr(i, "system", "") or "").lower():
            uhid = getattr(i, "value", None)
            break
    if uhid is None and idents:
        uhid = getattr(idents[0], "value", None)
    tel = getattr(pat, "telecom", None) or []
    return {
        "id": bare_id(pat.id),
        "patient_token": bare_id(pat.id),
        "first_name": given,
        "last_name": family,
        "uhid": uhid,
        "gender": getattr(pat, "gender", None),
        "dob": _dt(getattr(pat, "birthDate", None)),
        "phone": getattr(tel[0], "value", None) if tel else None,
    }


# ─── tasks / lab orders ──────────────────────────────────────────────────────────
def nursing_task(t) -> dict:
    ep = getattr(t, "executionPeriod", None)
    enc_ref = ref_id(getattr(t, "encounter", None)) or ref_id(getattr(t, "for_fhir", None))
    return {
        "id": bare_id(t.id),
        "admission_id": bare_id(enc_ref),
        "task": getattr(t, "description", None),
        "due_at": _dt(getattr(ep, "start", None) if ep else None),
        "assigned_to": ref_id(getattr(t, "owner", None)),
        "completed": getattr(t, "status", None) == "completed",
        "status": getattr(t, "status", None),
    }


def lab_order(sr) -> dict:
    code = getattr(sr, "code", None)               # R5 CodeableReference
    concept = getattr(code, "concept", None) if code else None
    return {
        "id": bare_id(sr.id),
        "patient_token": bare_id(ref_id(getattr(sr, "subject", None))) or "",
        "status": getattr(sr, "status", None),
        "priority": getattr(sr, "priority", None),
        "ordered_at": _dt(getattr(sr, "authoredOn", None)),
        "test_name": getattr(concept, "text", None) if concept else None,
    }


# ─── lab samples / analyzers (streamed; backend caches them) ─────────────────────
def lab_sample(specimen) -> dict:
    idents = getattr(specimen, "identifier", None) or []
    barcode = getattr(idents[0], "value", None) if idents else None
    requests = getattr(specimen, "request", None) or []
    order_id = bare_id(ref_id(requests[0])) if requests else None
    coll = getattr(specimen, "collection", None)
    collected_at = _dt(getattr(coll, "collectedDateTime", None)) if coll else None
    spec_type = getattr(specimen, "type", None)
    containers = getattr(specimen, "container", None) or []
    container_type = None
    if containers:
        ct = getattr(containers[0], "type", None)
        container_type = getattr(ct, "text", None) if ct else None
    return {
        "id": bare_id(specimen.id),
        "order_id": order_id,
        "patient_token": bare_id(ref_id(getattr(specimen, "subject", None))) or "",
        "barcode": barcode,
        "status": getattr(specimen, "status", None),
        "type": getattr(spec_type, "text", None) if spec_type else None,
        "collected_at": collected_at,
        "received_at": _dt(getattr(specimen, "receivedTime", None)),
        "container_type": container_type,
    }


def lab_analyzer(device) -> dict:
    names = getattr(device, "deviceName", None) or []
    name = getattr(names[0], "name", None) if names else None
    # R5 Device.manufacturer is a string, not a reference
    manufacturer = getattr(device, "manufacturer", None)
    if not isinstance(manufacturer, str):
        manufacturer = getattr(manufacturer, "display", None) if manufacturer else None
    loc = getattr(device, "location", None)
    return {
        "id": bare_id(device.id),
        "name": name,
        "model": getattr(device, "modelNumber", None),
        "manufacturer": manufacturer,
        "status": getattr(device, "status", None),
        "location_id": bare_id(ref_id(loc)) if loc else None,
    }


# ─── pharmacy orders / inventory (streamed; backend caches them) ─────────────────
def pharmacy_order(med_req) -> dict:
    med = getattr(med_req, "medication", None)          # R5 CodeableReference
    concept = getattr(med, "concept", None) if med else None
    med_display = getattr(concept, "text", None) if concept else None
    if not med_display:
        med_display = getattr(med, "display", None) if med else None
    dosage_list = getattr(med_req, "dosageInstruction", None) or []
    dosage = getattr(dosage_list[0], "text", None) if dosage_list else None
    return {
        "id": bare_id(med_req.id),
        "patient_token": bare_id(ref_id(getattr(med_req, "subject", None))) or "",
        "encounter_id": bare_id(ref_id(getattr(med_req, "encounter", None))),
        "prescriber_id": bare_id(ref_id(getattr(med_req, "requester", None))),
        "medication": med_display,
        "status": getattr(med_req, "status", None),
        "intent": getattr(med_req, "intent", None),
        "priority": getattr(med_req, "priority", None),
        "dosage": dosage,
        "prescribed_at": _dt(getattr(med_req, "authoredOn", None)),
    }


def pharmacy_inventory(inv_item) -> dict:
    names = getattr(inv_item, "name", None) or []
    name = getattr(names[0], "name", None) if names else None
    codes = getattr(inv_item, "code", None) or []
    code_concept = getattr(codes[0], "concept", None) if codes else None
    code_text = getattr(code_concept, "text", None) if code_concept else None
    categories = getattr(inv_item, "category", None) or []
    category = getattr(categories[0], "text", None) if categories else None
    net = getattr(inv_item, "netContent", None)
    instance = getattr(inv_item, "instance", None)
    expiry = None
    if instance:
        if isinstance(instance, list):
            instance = instance[0] if instance else None
        expiry = _dt(getattr(instance, "expiry", None)) if instance else None
    return {
        "id": bare_id(inv_item.id),
        "name": name,
        "code": code_text,
        "status": getattr(inv_item, "status", None),
        "category": category,
        "qty_in_stock": getattr(net, "value", None) if net else None,
        "unit": getattr(net, "unit", None) if net else None,
        "expiry_date": expiry,
    }
