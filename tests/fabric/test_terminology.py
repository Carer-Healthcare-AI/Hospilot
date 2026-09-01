"""Terminology maps + coders (fhirgw/terminology.py).

The single source of truth for the FHIR codings the mappers emit. These are the
plain-string -> coding translations the outbound API and search both rely on, so
a silent change to a code, or a reverse map that stops agreeing with the forward
map, is a data-contract bug rather than a cosmetic one.
"""

import pytest

from fhirgw import terminology as t


# ── vitals LOINC catalog ─────────────────────────────────────────────────────

def test_vitals_codes_covers_every_vital_and_bp_component():
    """VITALS_CODES drives `GET /fhir/Observation/{id}` dispatch. A vital whose
    code is missing here becomes unreadable by id even though it maps fine."""
    for code, _display, _unit in t.VITALS_LOINC.values():
        assert code in t.VITALS_CODES
    for code, *_ in (t.BP_PANEL_CODE, t.BP_SYSTOLIC, t.BP_DIASTOLIC):
        assert code in t.VITALS_CODES


def test_every_vital_carries_a_code_display_and_ucum_unit():
    for field, entry in t.VITALS_LOINC.items():
        code, display, unit = entry
        assert code and display and unit, f"{field} has an empty coding element"


def test_vitals_loinc_codes_are_unique():
    """Two vitals sharing a LOINC code would make read-by-code ambiguous."""
    codes = [c for c, _d, _u in t.VITALS_LOINC.values()]
    assert len(codes) == len(set(codes))


def test_spo2_carries_the_required_magic_pulse_ox_coding():
    """The FHIR `oxygensat` profile rejects the resource unless LOINC 2708-6 is
    present alongside our specific pulse-ox code."""
    spo2_code = t.VITALS_LOINC["spo2"][0]
    assert spo2_code == "59408-5"
    assert t.VITAL_REQUIRED_CODINGS[spo2_code] == (
        "2708-6", "Oxygen saturation in Arterial blood",
    )


def test_gcs_code_agrees_with_the_catalog():
    """GCS is emitted as valueInteger keyed off GCS_CODE; if that drifts from the
    catalog entry the score silently stops being recognised as a GCS."""
    assert t.GCS_CODE == t.VITALS_LOINC["gcs"][0]


# ── encounter status round-trip ──────────────────────────────────────────────

@pytest.mark.parametrize("internal,fhir", [
    ("admitted", "in-progress"),
    ("discharging", "discharged"),
    ("in_treatment", "in-progress"),
    ("discharged", "completed"),
    ("waiting", "in-progress"),
    ("triaged", "in-progress"),
    ("cancelled", "cancelled"),
])
def test_encounter_status_to_fhir(internal, fhir):
    assert t.encounter_status_to_fhir(internal) == fhir


def test_encounter_status_to_fhir_is_case_insensitive_and_defaults_unknown():
    assert t.encounter_status_to_fhir("ADMITTED") == "in-progress"
    assert t.encounter_status_to_fhir("Discharged") == "completed"
    assert t.encounter_status_to_fhir("nonsense") == "unknown"
    assert t.encounter_status_to_fhir(None) == "unknown"
    assert t.encounter_status_to_fhir("") == "unknown"


def test_encounter_status_reverse_map_is_consistent():
    """Every internal status must be recoverable from the FHIR status it maps to.
    This is what makes `?status=` search return the same rows the mapper wrote."""
    for internal, fhir in t.ENCOUNTER_STATUS_MAP.items():
        assert internal in t.encounter_status_to_internal(fhir), (
            f"{internal!r} -> {fhir!r} but the reverse map loses it"
        )


def test_encounter_status_to_internal_unknown_passes_through():
    """An unmapped status is echoed back so a search for it yields nothing rather
    than silently matching everything."""
    assert t.encounter_status_to_internal("planned") == ["planned"]


# ── lab interpretation ───────────────────────────────────────────────────────

def test_interpretation_map_covers_the_hospilot_flags():
    assert t.INTERPRETATION_MAP["Normal"] == ("N", "Normal")
    assert t.INTERPRETATION_MAP["Low"] == ("L", "Low")
    assert t.INTERPRETATION_MAP["High"] == ("H", "High")
    assert t.INTERPRETATION_MAP["Critical"] == ("HH", "Critical high")


def test_critical_vital_interpretation_is_distinct_from_the_lab_critical():
    """A critical vital is AA (abnormal), a critical lab is HH (high). Collapsing
    them would misreport one of the two."""
    assert t.CRITICAL_INTERP == ("AA", "Critical abnormal")
    assert t.CRITICAL_INTERP != t.INTERPRETATION_MAP["Critical"]


# ── location status + operational status ─────────────────────────────────────

def test_location_status():
    assert t.location_status(True, "Available") == "active"
    assert t.location_status(True, "Occupied") == "active"
    assert t.location_status(True, "Dirty") == "suspended"
    assert t.location_status(True, "Cleaning") == "suspended"
    assert t.location_status(False, "Available") == "inactive"


def test_location_status_inactive_wins_over_a_suspended_status():
    """A decommissioned bed is inactive regardless of how dirty it is."""
    assert t.location_status(False, "Dirty") == "inactive"


def test_operational_status_maps_bed_status_and_is_case_insensitive():
    assert t.operational_status("Available") == ("U", "Unoccupied")
    assert t.operational_status("available") == ("U", "Unoccupied")
    assert t.operational_status("OCCUPIED") == ("O", "Occupied")
    assert t.operational_status("Dirty") == ("K", "Contaminated")
    assert t.operational_status("Cleaning") == ("H", "Housekeeping")


def test_operational_status_unknown_is_none_not_a_guess():
    """An unrecognised bed status must omit operationalStatus rather than assert a
    wrong one — a bed wrongly coded Unoccupied would be offered to a patient."""
    assert t.operational_status("teleported") is None
    assert t.operational_status(None) is None
    assert t.operational_status("") is None


def test_reserved_and_vacating_beds_are_not_free():
    """Both are transitional but neither is allocatable; they must code Occupied."""
    assert t.operational_status("reserved") == ("O", "Occupied")
    assert t.operational_status("vacating") == ("O", "Occupied")


# ── triage priority ──────────────────────────────────────────────────────────

def test_triage_priority_covers_ctas_1_to_5_and_is_monotonic():
    """CTAS 1 is the most urgent; urgency must never increase as the number grows."""
    assert set(t.TRIAGE_PRIORITY) == {1, 2, 3, 4, 5}
    rank = {"EM": 0, "UR": 1, "R": 2}
    codes = [t.TRIAGE_PRIORITY[i][0] for i in range(1, 6)]
    assert codes == ["EM", "UR", "UR", "R", "R"]
    assert rank[codes[0]] <= rank[codes[-1]]
