"""FHIR identifier/reference helpers (fhirgw/identifiers.py).

These build the namespaced `system` URIs and relative references every mapper
depends on. Identifier systems are derived from settings.fhir_base_url so they
are stable per-deployment — assertions are made relative to that base rather
than hard-coding a host.
"""

from config import settings
from fhirgw import identifiers as ident

BASE = settings.fhir_base_url.rstrip("/")

# Every system helper, with the suffix each one owns.
SYSTEMS = [
    (ident.sys_patient_token, "/identifier/patient-token"),
    (ident.sys_admission,     "/identifier/admission"),
    (ident.sys_visit,         "/identifier/visit"),
    (ident.sys_bed,           "/identifier/bed"),
    (ident.sys_department,    "/identifier/department"),
    (ident.sys_organization,  "/identifier/organization"),
    (ident.sys_vital,         "/identifier/vital"),
    (ident.sys_lab,           "/identifier/lab-result"),
    (ident.sys_lab_code,      "/CodeSystem/lab-test-code"),
]


def test_reference_is_a_relative_fhir_reference():
    assert ident.reference("Patient", "p1") == {"reference": "Patient/p1"}
    assert ident.patient_reference("tok-1") == {"reference": "Patient/tok-1"}


def test_reference_has_no_absolute_url():
    """Relative references keep the bundle portable across deployments."""
    ref = ident.reference("Location", "bed-1")["reference"]
    assert not ref.startswith("http")
    assert ref.count("/") == 1


def test_identifier_stringifies_value():
    """Ids arrive as UUIDs, ints or strings; FHIR requires a string value."""
    assert ident.identifier("sys", 42) == {"system": "sys", "value": "42"}
    assert ident.identifier("sys", "abc")["value"] == "abc"


def test_reference_stringifies_non_string_ids():
    assert ident.reference("Encounter", 7) == {"reference": "Encounter/7"}


def test_patient_identifier_uses_the_patient_token_system():
    got = ident.patient_identifier("tok-9")
    assert got == {"system": ident.sys_patient_token(), "value": "tok-9"}


def test_identifier_systems_share_the_configured_base():
    for fn, suffix in SYSTEMS:
        assert fn() == f"{BASE}{suffix}", f"{fn.__name__} drifted from the base"


def test_identifier_systems_are_all_distinct():
    """Admission-vs-visit and vital-vs-lab need separate systems so a future
    write-back can route a resource to the correct Hospilot table."""
    values = [fn() for fn, _ in SYSTEMS]
    assert len(values) == len(set(values))


def test_source_organization_reference_tracks_ehr_source():
    oid = ident.source_organization_id()
    assert oid == f"source-{settings.ehr_source}"
    assert ident.source_organization_reference() == {
        "reference": f"Organization/{oid}"
    }
