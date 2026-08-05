"""
Hospilot FHIR extensions.

Hospilot enrichment columns that have no native home on a base FHIR resource
ride along as `extension[]`. We also round-trip a few *operational* values
(e.g. the raw bed status string) via extensions so `to_internal(to_fhir(x))`
reproduces the exact projection agents consume -- see the round-trip tests.

Helpers accept either fhir.resources model instances or plain dicts when
reading, so they work whether you hold a parsed model or a model_dump().
"""

EXT_BASE = "https://hospilot.carer.ai/fhir/StructureDefinition/"

# Bed (Location) enrichment + round-tripped operational fields
EXT_BED_VENTILATION    = EXT_BASE + "bed-ventilation"
EXT_BED_ROOM_SHARING   = EXT_BASE + "bed-room-sharing"
EXT_BED_PROXIMITY      = EXT_BASE + "bed-proximity"
EXT_BED_FLOOR          = EXT_BASE + "bed-floor"
EXT_BED_WING           = EXT_BASE + "bed-wing"
EXT_BED_NATURAL_LIGHT  = EXT_BASE + "bed-natural-light"
EXT_BED_NOISE_LEVEL    = EXT_BASE + "bed-noise-level"
EXT_BED_FEATURE        = EXT_BASE + "bed-feature"        # repeated, one per feature
EXT_BED_RAW_STATUS     = EXT_BASE + "bed-raw-status"     # preserves "reserved"/"Dirty"/etc.
EXT_BED_WARD           = EXT_BASE + "bed-ward"
EXT_BED_ROOM_TYPE      = EXT_BASE + "bed-room-type"
EXT_BED_BRANCH_ID      = EXT_BASE + "bed-branch-id"

# Admission (Encounter IMP) -- enrichment + lossless round-trip of projection fields
EXT_ADM_DISCHARGE_READY          = EXT_BASE + "admission-discharge-ready"
EXT_ADM_DISCHARGE_BLOCKED_REASON = EXT_BASE + "admission-discharge-blocked-reason"
EXT_ADM_EXPECTED_DISCHARGE       = EXT_BASE + "admission-expected-discharge-at"  # raw string
EXT_ADM_ADMITTED_AT              = EXT_BASE + "admission-admitted-at"            # raw string
EXT_ADM_RAW_STATUS               = EXT_BASE + "admission-raw-status"            # internal status code

# Visit (Encounter EMER) -- enrichment + lossless round-trip of projection fields
EXT_VISIT_TRIAGE_SCORE  = EXT_BASE + "visit-triage-score"
EXT_VISIT_ARRIVED_AT    = EXT_BASE + "visit-arrived-at"    # raw string
EXT_VISIT_RAW_STATUS    = EXT_BASE + "visit-raw-status"    # internal status code

# Organization (department)
EXT_ORG_RAW_TYPE = EXT_BASE + "organization-raw-type"

# Vital (Observation) -- enrichment + lossless round-trip
EXT_VITAL_IS_CRITICAL  = EXT_BASE + "vital-is-critical"
EXT_VITAL_ADMISSION_ID = EXT_BASE + "vital-admission-id"
EXT_VITAL_RECORDED_AT  = EXT_BASE + "vital-recorded-at"   # raw string


# --- builders -----------------------------------------------------------------
def ext_string(url: str, value) -> dict:
    return {"url": url, "valueString": str(value)}


def ext_int(url: str, value) -> dict:
    return {"url": url, "valueInteger": int(value)}


def ext_bool(url: str, value) -> dict:
    return {"url": url, "valueBoolean": bool(value)}


def ext_decimal(url: str, value) -> dict:
    return {"url": url, "valueDecimal": value}


_VALUE_FIELDS = (
    "valueString", "valueInteger", "valueBoolean", "valueDecimal",
    "valueDateTime", "valueCode",
)


def _url_of(e) -> str | None:
    return e.get("url") if isinstance(e, dict) else getattr(e, "url", None)


def _value_of(e):
    if isinstance(e, dict):
        for f in _VALUE_FIELDS:
            if e.get(f) is not None:
                return e[f]
        return None
    for f in _VALUE_FIELDS:
        v = getattr(e, f, None)
        if v is not None:
            return v
    return None


def get_ext(extensions, url: str):
    """First extension value matching `url`, or None. Works on models or dicts."""
    if not extensions:
        return None
    for e in extensions:
        if _url_of(e) == url:
            return _value_of(e)
    return None


def get_ext_all(extensions, url: str) -> list:
    """All values for a repeated extension (e.g. bed features)."""
    out: list = []
    if not extensions:
        return out
    for e in extensions:
        if _url_of(e) == url:
            v = _value_of(e)
            if v is not None:
                out.append(v)
    return out
