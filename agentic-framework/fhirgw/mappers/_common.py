"""Shared read helpers that work on either fhir.resources model instances or dicts."""

from decimal import Decimal


def _coerce_decimal(v):
    """fhir.resources stores Quantity.value as decimal.Decimal. Convert to a plain
    Python numeric so JSON serialization (httpx / json.dumps) doesn't fail."""
    if not isinstance(v, Decimal):
        return v
    f = float(v)
    return int(f) if f == int(f) else f


def parse_dt_safe(value):
    """Return value only if it's a FHIR-acceptable dateTime (tz-aware when timed).

    FHIR rejects naive timestamps like '2025-01-15T10:30:00' and normalizes the
    rest, so we set proper FHIR dateTime fields only when safe and always keep
    the raw string in an extension for lossless to_internal recovery.
    """
    if not value or not isinstance(value, str):
        return None
    if "T" in value:
        tail = value.split("T", 1)[1]
        if not (value.endswith("Z") or "+" in tail or "-" in tail):
            return None  # naive -> would raise; rely on the extension instead
    return value


def ref_id(reference_obj):
    """'Patient/123' -> '123' from a Reference (model or dict). None if absent."""
    if reference_obj is None:
        return None
    ref = reference_obj.get("reference") if isinstance(reference_obj, dict) else getattr(reference_obj, "reference", None)
    if not ref or "/" not in ref:
        return None
    return ref.split("/", 1)[1]


# CarerOS prefixes its FHIR resource ids by type (e.g. `em-<uuid>`, `ipd-<uuid>`,
# `bed-<uuid>`, `spo2-<uuid>`). Hospilot-owned writes key on the bare DB uuid, so
# strip a known prefix before writing back. (Bare Hospilot uuids have no such
# prefix and pass through unchanged.)
_FHIR_ID_PREFIXES = ("ipd-", "em-", "bed-", "ward-", "spo2-", "hr-", "rr-",
                     "temp-", "bp-", "gcs-", "lab-")


def bare_id(fhir_id: str | None) -> str | None:
    """Recover the bare DB key from a (possibly CarerOS-prefixed) FHIR id."""
    if not fhir_id:
        return fhir_id
    for p in _FHIR_ID_PREFIXES:
        if fhir_id.startswith(p):
            return fhir_id[len(p):]
    return fhir_id


def location_ref_id(location_list):
    if not location_list:
        return None
    first = location_list[0]
    loc = first.get("location") if isinstance(first, dict) else getattr(first, "location", None)
    return ref_id(loc)


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def reason_text(reason_list):
    """Chief-complaint text from R5 Encounter.reason[].value[].concept.text.

    R5 changed reasonCode (CodeableConcept[]) to reason[] whose `value` is a
    CodeableReference (concept | reference). We carry free text in concept.text.
    """
    if not reason_list:
        return None
    value = _get(reason_list[0], "value")
    if not value:
        return None
    concept = _get(value[0], "concept")
    if concept is None:
        return None
    return _get(concept, "text")


def coding_code(codeable_concept):
    """First coding.code from a CodeableConcept (model or dict)."""
    if codeable_concept is None:
        return None
    coding = codeable_concept.get("coding") if isinstance(codeable_concept, dict) else getattr(codeable_concept, "coding", None)
    if not coding:
        return None
    first = coding[0]
    return first.get("code") if isinstance(first, dict) else getattr(first, "code", None)


def qty_value(value_quantity):
    if value_quantity is None:
        return None
    v = value_quantity.get("value") if isinstance(value_quantity, dict) else getattr(value_quantity, "value", None)
    return _coerce_decimal(v)


def num(value):
    """Coerce a possibly-stringified or Decimal numeric to a plain Python number."""
    if isinstance(value, Decimal):
        return _coerce_decimal(value)
    if isinstance(value, str):
        try:
            f = float(value)
            return int(f) if f == int(f) else f
        except ValueError:
            return None
    return value
