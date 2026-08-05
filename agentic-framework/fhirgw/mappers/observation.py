"""vitals/labs <-> FHIR Observation.

A single vitals row becomes a SET of Observations (one per LOINC vital, plus a
blood-pressure panel with two components). Each carries a deterministic composite
id `{vital_id}.{loinc}` (uuid and loinc both contain '-', so we join with '.',
which neither contains and FHIR ids permit).

Lab results map one-to-one to a laboratory Observation (pilot: read-only / outbound).
"""

from fhir.resources.observation import Observation

from fhirgw import terminology as T, extensions as X, identifiers as ID, narrative as N
from fhirgw.mappers._common import parse_dt_safe, ref_id, coding_code, qty_value, num


def _quantity(value, unit: str) -> dict:
    v = num(value) if isinstance(value, str) else value
    return {"value": v, "unit": unit, "system": T.SYS_UCUM, "code": unit}


def _loinc_code(code: str, display: str) -> dict:
    """code CodeableConcept, appending any profile-required "magic" coding."""
    coding = [{"system": T.SYS_LOINC, "code": code, "display": display}]
    extra = T.VITAL_REQUIRED_CODINGS.get(code)
    if extra:
        coding.append({"system": T.SYS_LOINC, "code": extra[0], "display": extra[1]})
    return {"coding": coding}


def _shared(vital: dict) -> dict:
    exts: list[dict] = []
    if vital.get("recorded_at") is not None:
        exts.append(X.ext_string(X.EXT_VITAL_RECORDED_AT, vital["recorded_at"]))
    if vital.get("admission_id") is not None:
        exts.append(X.ext_string(X.EXT_VITAL_ADMISSION_ID, vital["admission_id"]))
    if vital.get("is_critical") is not None:
        exts.append(X.ext_bool(X.EXT_VITAL_IS_CRITICAL, vital["is_critical"]))

    shared: dict = {"performer": [ID.source_organization_reference()]}
    if vital.get("patient_token"):
        shared["subject"] = ID.patient_reference(vital["patient_token"])
    if exts:
        shared["extension"] = exts
    eff = parse_dt_safe(vital.get("recorded_at"))
    if eff:
        shared["effectiveDateTime"] = eff
    if vital.get("is_critical"):
        shared["interpretation"] = [{"coding": [{
            "system": T.SYS_INTERPRETATION,
            "code": T.CRITICAL_INTERP[0], "display": T.CRITICAL_INTERP[1],
        }]}]
    return shared


def _vital_summary(display: str, value_quantity, value_integer, component) -> str:
    if value_quantity is not None:
        return f"{display}: {value_quantity['value']} {value_quantity.get('unit', '')}".strip()
    if value_integer is not None:
        return f"{display}: {value_integer}"
    if component:
        parts = [f"{c['code']['coding'][0]['display']} {c['valueQuantity']['value']} "
                 f"{c['valueQuantity'].get('unit', '')}".strip() for c in component]
        return f"{display}: " + ", ".join(parts)
    return display


def _vital_obs(vid: str, code: str, display: str, shared: dict, *, value_quantity=None, value_integer=None, component=None) -> Observation:
    cid = f"{vid}.{code}"
    kwargs: dict = {
        "id": cid,
        "identifier": [ID.identifier(ID.sys_vital(), cid)],
        "status": "final",
        "code": _loinc_code(code, display),
        "category": [{"coding": [{"system": T.SYS_OBS_CATEGORY, "code": T.OBS_CAT_VITAL_SIGNS}]}],
        "text": N.text(_vital_summary(display, value_quantity, value_integer, component)),
        **shared,
    }
    if value_quantity is not None:
        kwargs["valueQuantity"] = value_quantity
    if value_integer is not None:
        kwargs["valueInteger"] = value_integer
    if component is not None:
        kwargs["component"] = component
    return Observation(**kwargs)


def vitals_to_fhir(vital: dict) -> list[Observation]:
    vid = str(vital["id"])
    shared = _shared(vital)
    out: list[Observation] = []

    for field, (code, display, unit) in T.VITALS_LOINC.items():
        val = vital.get(field)
        if val is None:
            continue
        if code == T.GCS_CODE:  # unit-less integer score -> valueInteger
            out.append(_vital_obs(vid, code, display, shared, value_integer=int(num(val))))
        else:
            out.append(_vital_obs(vid, code, display, shared, value_quantity=_quantity(val, unit)))

    sysv, diav = vital.get("bp_systolic"), vital.get("bp_diastolic")
    if sysv is not None or diav is not None:
        comps = []
        if sysv is not None:
            comps.append({"code": {"coding": [{"system": T.SYS_LOINC, "code": T.BP_SYSTOLIC[0], "display": T.BP_SYSTOLIC[1]}]},
                          "valueQuantity": _quantity(sysv, T.BP_SYSTOLIC[2])})
        if diav is not None:
            comps.append({"code": {"coding": [{"system": T.SYS_LOINC, "code": T.BP_DIASTOLIC[0], "display": T.BP_DIASTOLIC[1]}]},
                          "valueQuantity": _quantity(diav, T.BP_DIASTOLIC[2])})
        out.append(_vital_obs(vid, T.BP_PANEL_CODE[0], T.BP_PANEL_CODE[1], shared, component=comps))

    return out


# Operational columns the hospilot_vitals upsert may write (excludes the
# Hospilot-owned `is_critical` enrichment, matching the poller's old _map_vital).
VITAL_UPSERT_COLUMNS = (
    "id", "patient_token", "admission_id", "recorded_at",
    "temperature", "pulse", "bp_systolic", "bp_diastolic",
    "spo2", "respiratory_rate", "gcs",
)


def to_vital_upsert_rows(observations) -> list[dict]:
    """Group a flat list of vital Observations back into operational upsert rows."""
    groups: dict[str, list] = {}
    for o in observations:
        vid = (o.id or "").split(".")[0]
        groups.setdefault(vid, []).append(o)
    rows = []
    for group in groups.values():
        full = vitals_to_internal(group)
        rows.append({k: full.get(k) for k in VITAL_UPSERT_COLUMNS})
    return rows


def vitals_to_internal(observations) -> dict:
    obs_list = list(observations)
    if not obs_list:
        return {}
    first = obs_list[0]
    vid = (first.id or "").split(".")[0]
    result = {
        "id": vid,
        "patient_token": ref_id(first.subject) or "",
        "admission_id": X.get_ext(first.extension, X.EXT_VITAL_ADMISSION_ID),
        "recorded_at": X.get_ext(first.extension, X.EXT_VITAL_RECORDED_AT),
        "temperature": None, "pulse": None, "bp_systolic": None, "bp_diastolic": None,
        "spo2": None, "respiratory_rate": None, "gcs": None,
        "is_critical": X.get_ext(first.extension, X.EXT_VITAL_IS_CRITICAL),
    }
    loinc_to_field = {code: field for field, (code, _d, _u) in T.VITALS_LOINC.items()}
    for obs in obs_list:
        code = coding_code(obs.code)
        if code in loinc_to_field:
            field = loinc_to_field[code]
            if field == "gcs":  # emitted as valueInteger, not a Quantity
                result[field] = num(getattr(obs, "valueInteger", None) if not isinstance(obs, dict) else obs.get("valueInteger"))
            else:
                result[field] = qty_value(obs.valueQuantity)
        elif code == T.BP_PANEL_CODE[0]:
            for comp in (obs.component or []):
                ccode = coding_code(comp.code if not isinstance(comp, dict) else comp.get("code"))
                cvq = comp.valueQuantity if not isinstance(comp, dict) else comp.get("valueQuantity")
                if ccode == T.BP_SYSTOLIC[0]:
                    result["bp_systolic"] = qty_value(cvq)
                elif ccode == T.BP_DIASTOLIC[0]:
                    result["bp_diastolic"] = qty_value(cvq)
    return result


# --- labs (one row -> one laboratory Observation) -----------------------------
def lab_result_to_observation(row: dict) -> Observation:
    code = row.get("test_code")
    name = row.get("test_name")
    _val = row.get("result_value")
    _unit = row.get("unit")
    _label = name or (str(code) if code else "Lab result")
    _summary = f"{_label}: {_val}" + (f" {_unit}" if _val is not None and _unit else "") if _val is not None else _label
    kwargs: dict = {
        "id": str(row["id"]),
        "identifier": [ID.identifier(ID.sys_lab(), row["id"])],
        "status": "final",
        "category": [{"coding": [{"system": T.SYS_OBS_CATEGORY, "code": T.OBS_CAT_LABORATORY}]}],
        "performer": [ID.source_organization_reference()],
        "text": N.text(_summary),
        "code": {
            "coding": ([{"system": ID.sys_lab_code(), "code": str(code), "display": name}] if code else None),
            "text": name,
        },
    }
    if not code:
        kwargs["code"] = {"text": name or "Unknown lab test"}
    if row.get("patient_token"):
        kwargs["subject"] = ID.patient_reference(row["patient_token"])
    eff = parse_dt_safe(row.get("reported_at"))
    if eff:
        kwargs["effectiveDateTime"] = eff

    val = row.get("result_value")
    unit = row.get("unit")
    fval = num(val) if isinstance(val, str) else val
    if isinstance(fval, (int, float)) and unit:
        kwargs["valueQuantity"] = {"value": fval, "unit": unit, "system": T.SYS_UCUM, "code": unit}
    elif val is not None:
        kwargs["valueString"] = str(val)

    flag = row.get("flag")
    if flag in T.INTERPRETATION_MAP:
        c, d = T.INTERPRETATION_MAP[flag]
        kwargs["interpretation"] = [{"coding": [{"system": T.SYS_INTERPRETATION, "code": c, "display": d}]}]
    rr = row.get("reference_range")
    if rr:
        kwargs["referenceRange"] = [{"text": str(rr)}]
    return Observation(**kwargs)
