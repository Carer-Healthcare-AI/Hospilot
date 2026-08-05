"""
Terminology: code systems and the maps that turn Hospilot's plain strings/enums
into FHIR codings (and back, for search). This module is the single source of
truth for systems + codes used by both the mappers and the outbound API.
"""

# --- Code systems ------------------------------------------------------------
SYS_LOINC                 = "http://loinc.org"
SYS_SNOMED                = "http://snomed.info/sct"
SYS_UCUM                  = "http://unitsofmeasure.org"
SYS_OBS_CATEGORY          = "http://terminology.hl7.org/CodeSystem/observation-category"
SYS_INTERPRETATION        = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
SYS_ENCOUNTER_CLASS       = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
SYS_LOCATION_PHYSICAL     = "http://terminology.hl7.org/CodeSystem/location-physical-type"
SYS_LOCATION_OPER_STATUS  = "http://terminology.hl7.org/CodeSystem/v2-0116"
SYS_ORG_TYPE              = "http://terminology.hl7.org/CodeSystem/organization-type"
SYS_SECURITY              = "http://terminology.hl7.org/CodeSystem/v3-ObservationValue"
# Pseudonymization security label (member of the All Security Labels value set).
# Code is PSEUDED in v3-ObservationValue -- NOT "PSEUD" in v3-ActReason (invalid).
PSEUD_SECURITY_LABEL      = ("PSEUDED", "pseudonymized")

# --- Observation categories ---------------------------------------------------
OBS_CAT_VITAL_SIGNS = "vital-signs"
OBS_CAT_LABORATORY  = "laboratory"

# --- Vital signs LOINC: hospilot field -> (code, display, UCUM unit) ----------
# Field names match the internal vitals projection (see poller _map_vital).
VITALS_LOINC: dict[str, tuple[str, str, str]] = {
    "temperature":      ("8310-5",  "Body temperature",  "Cel"),
    "pulse":            ("8867-4",  "Heart rate",        "/min"),
    "respiratory_rate": ("9279-1",  "Respiratory rate",  "/min"),
    "spo2":             ("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
    "gcs":              ("9269-2",  "Glasgow coma score total", "{score}"),
}
# Blood pressure is a panel with two components.
BP_PANEL_CODE   = ("85354-9", "Blood pressure panel with all children optional")
BP_SYSTOLIC     = ("8480-6",  "Systolic blood pressure",  "mm[Hg]")
BP_DIASTOLIC    = ("8462-4",  "Diastolic blood pressure", "mm[Hg]")

# The FHIR `oxygensat` profile requires the "magic" LOINC code 2708-6 to be
# present on Observation.code (it rejects the resource otherwise) and that code
# -- not the pulse-ox code 59408-5 -- is the one in the Vital Signs value set. We
# carry both: our specific pulse-ox code first (so round-trip keys off it) plus
# the required magic code. Map of primary LOINC -> extra required (code, display).
VITAL_REQUIRED_CODINGS: dict[str, tuple[str, str]] = {
    "59408-5": ("2708-6", "Oxygen saturation in Arterial blood"),
}

# GCS total is a unit-less integer score. Emitting it as a Quantity forces a
# UCUM annotation like {score}, which the validator warns is misleading, so we
# emit valueInteger instead (9269-2 is not one of the profiled vital codes).
GCS_CODE = "9269-2"

# Set of LOINC codes that belong to a single vitals reading (for /fhir read by id)
VITALS_CODES = {c for c, _d, _u in VITALS_LOINC.values()} | {
    BP_PANEL_CODE[0], BP_SYSTOLIC[0], BP_DIASTOLIC[0],
}

# --- Lab interpretation: hospilot `flag` -> (code, display) -------------------
INTERPRETATION_MAP: dict[str, tuple[str, str]] = {
    "Normal":   ("N",  "Normal"),
    "Low":      ("L",  "Low"),
    "High":     ("H",  "High"),
    "Critical": ("HH", "Critical high"),
}
# Vital `is_critical` flag -> interpretation
CRITICAL_INTERP = ("AA", "Critical abnormal")

# --- Encounter status: internal (lowercased) -> FHIR Encounter.status (R5) ----
# R5 EncounterStatus value set: planned | in-progress | on-hold | discharged |
# completed | cancelled | discontinued | entered-in-error | unknown.
# (R4's arrived/triaged/finished were removed in R5.) The raw internal status is
# preserved in an extension, so this lossy mapping never breaks the round-trip.
# Admissions: admitted | discharging | discharged
# ER visits:  waiting | triaged | in_treatment | admitted | discharged
ENCOUNTER_STATUS_MAP: dict[str, str] = {
    "admitted":     "in-progress",
    "discharging":  "discharged",   # patient being discharged; encounter finalizing
    "in_treatment": "in-progress",
    "discharged":   "completed",
    "waiting":      "in-progress",  # R5 has no 'arrived'; the encounter has begun
    "triaged":      "in-progress",  # R5 has no 'triaged'
    "cancelled":    "cancelled",
}
_FHIR_TO_INTERNAL_ENCOUNTER_STATUS = {
    "in-progress": ["admitted", "in_treatment", "waiting", "triaged"],
    "discharged":  ["discharging"],
    "completed":   ["discharged"],
    "cancelled":   ["cancelled"],
}


def encounter_status_to_fhir(internal_status: str | None) -> str:
    return ENCOUNTER_STATUS_MAP.get((internal_status or "").lower(), "unknown")


def encounter_status_to_internal(fhir_status: str) -> list[str]:
    """Reverse map for ?status= search (one FHIR status may match several internal)."""
    return _FHIR_TO_INTERNAL_ENCOUNTER_STATUS.get(fhir_status, [fhir_status])


# --- Encounter class (v3-ActCode) ---------------------------------------------
ENCOUNTER_CLASS_IMP  = {"system": SYS_ENCOUNTER_CLASS, "code": "IMP",  "display": "inpatient encounter"}
ENCOUNTER_CLASS_EMER = {"system": SYS_ENCOUNTER_CLASS, "code": "EMER", "display": "emergency"}

# --- Location operational status (HL7 v2-0116) from bed `status` string -------
# Hospilot bed statuses: Available | Occupied | reserved | vacating | Dirty | Cleaning
LOCATION_OPER_STATUS: dict[str, tuple[str, str]] = {
    "available": ("U", "Unoccupied"),
    "occupied":  ("O", "Occupied"),
    "reserved":  ("O", "Occupied"),
    "vacating":  ("O", "Occupied"),
    "dirty":     ("K", "Contaminated"),
    "cleaning":  ("H", "Housekeeping"),
}


def location_status(is_active: bool, bed_status: str | None) -> str:
    """FHIR Location.status: active | suspended | inactive."""
    if not is_active:
        return "inactive"
    if (bed_status or "").lower() in ("dirty", "cleaning"):
        return "suspended"
    return "active"


def operational_status(bed_status: str | None) -> tuple[str, str] | None:
    return LOCATION_OPER_STATUS.get((bed_status or "").lower())


# --- Triage (CTAS 1-5) -> FHIR Encounter.priority (very rough) ----------------
# CTAS 1 (resuscitation) is most urgent; FHIR priority uses ActPriority codes.
TRIAGE_PRIORITY: dict[int, tuple[str, str]] = {
    1: ("EM", "emergency"),
    2: ("UR", "urgent"),
    3: ("UR", "urgent"),
    4: ("R",  "routine"),
    5: ("R",  "routine"),
}
SYS_ACT_PRIORITY = "http://terminology.hl7.org/CodeSystem/v3-ActPriority"
