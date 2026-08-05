"""
The server CapabilityStatement (GET /fhir/metadata). Built once at import.

Declares the pilot resources and the search params each supports. Keep this in
sync with the search logic in api/fhir.py.
"""

from fhir.resources.capabilitystatement import CapabilityStatement

from fhirgw import FHIR_VERSION, narrative as N
from fhirgw.serialization import FHIR_MEDIA_TYPE

_BUILD_DATE = "2026-06-02"

_RESOURCES = [
    {"type": "Patient",
     "interaction": [{"code": "read"}, {"code": "search-type"}],
     "searchParam": [{"name": "identifier", "type": "token"}]},
    {"type": "Encounter",
     "interaction": [{"code": "read"}, {"code": "search-type"}],
     "searchParam": [
         {"name": "patient", "type": "reference"},
         {"name": "status", "type": "token"},
         {"name": "class", "type": "token"},
         {"name": "_count", "type": "number"},
     ]},
    {"type": "Observation",
     "interaction": [{"code": "read"}, {"code": "search-type"}],
     "searchParam": [
         {"name": "patient", "type": "reference"},
         {"name": "category", "type": "token"},
         {"name": "code", "type": "token"},
         {"name": "_count", "type": "number"},
     ]},
    {"type": "Location",
     "interaction": [{"code": "read"}, {"code": "search-type"}],
     "searchParam": [
         {"name": "status", "type": "token"},
         {"name": "type", "type": "token"},
         {"name": "_count", "type": "number"},
     ]},
    {"type": "Organization",
     "interaction": [{"code": "read"}, {"code": "search-type"}],
     "searchParam": [
         {"name": "type", "type": "token"},
         {"name": "_count", "type": "number"},
     ]},
    {"type": "StructureDefinition",
     "interaction": [{"code": "read"}, {"code": "search-type"}]},
]


def capability_statement() -> CapabilityStatement:
    return CapabilityStatement(
        status="active",
        date=_BUILD_DATE,
        kind="instance",
        text=N.text("Hospilot FHIR gateway -- pilot R5 server capability statement"),
        fhirVersion=FHIR_VERSION,
        format=["json", FHIR_MEDIA_TYPE],
        software={"name": "Hospilot FHIR gateway"},
        implementation={"description": "Hospilot in-app FHIR R5 API (pilot)"},
        rest=[{"mode": "server", "resource": _RESOURCES}],
    )


# Built once; the statement is static.
CAPABILITY_STATEMENT = capability_statement()
