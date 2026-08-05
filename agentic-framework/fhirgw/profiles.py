"""Published StructureDefinitions for Hospilot's custom extensions.

Every `hospilot.carer.ai` extension gets a formal `StructureDefinition` so FHIR
validators can resolve it. Without them a strict validator (and any R5 run)
reports each extension as *"could not be found so is not allowed here"*; with
them loaded the extensions validate cleanly.

Use them either way:
  * load into the HL7 validator:  `validator_cli.jar resource.json -version 4.3 -ig fhir-ig/`
  * or fetch live:                `GET /fhir/StructureDefinition/{id}`

The single source of truth for which extensions exist is `_EXT_SPECS` here;
`tests/test_fhirgw_foundation.py` asserts every extension URL the mappers emit
has a matching published definition, so the two can't drift.
"""

from fhir.resources.structuredefinition import StructureDefinition

from fhirgw import FHIR_VERSION, extensions as X

EXTENSION_BASE = "http://hl7.org/fhir/StructureDefinition/Extension"

# suffix (on X.EXT_BASE) -> (value type, context resource, title)
_EXT_SPECS: list[tuple[str, str, str, str]] = [
    # Observation -- vital signs
    ("vital-recorded-at",  "dateTime", "Observation",  "Vital recorded-at timestamp"),
    ("vital-admission-id", "string",   "Observation",  "Admission id the vital belongs to"),
    ("vital-is-critical",  "boolean",  "Observation",  "Hospilot critical-vital flag"),
    # Encounter -- inpatient admission (IMP)
    ("admission-admitted-at",            "dateTime", "Encounter", "Admission start timestamp"),
    ("admission-expected-discharge-at",  "dateTime", "Encounter", "Expected discharge timestamp"),
    ("admission-raw-status",             "string",   "Encounter", "Internal admission status"),
    ("admission-discharge-ready",        "boolean",  "Encounter", "Discharge-ready flag"),
    ("admission-discharge-blocked-reason","string",  "Encounter", "Discharge blocked reason"),
    # Encounter -- ER visit (EMER)
    ("visit-arrived-at",   "dateTime", "Encounter", "Visit arrival timestamp"),
    ("visit-raw-status",   "string",   "Encounter", "Internal visit status"),
    ("visit-triage-score", "integer",  "Encounter", "CTAS triage score"),
    # Organization -- department
    ("organization-raw-type", "string", "Organization", "Internal department type"),
    # Location -- bed
    ("bed-ward",          "string",  "Location", "Ward"),
    ("bed-room-type",     "string",  "Location", "Room type"),
    ("bed-branch-id",     "string",  "Location", "Branch id"),
    ("bed-raw-status",    "string",  "Location", "Raw bed status string"),
    ("bed-ventilation",   "string",  "Location", "Ventilation capability"),
    ("bed-room-sharing",  "string",  "Location", "Room sharing"),
    ("bed-wing",          "string",  "Location", "Wing"),
    ("bed-noise-level",   "string",  "Location", "Noise level"),
    ("bed-proximity",     "integer", "Location", "Nurse-station proximity rank"),
    ("bed-floor",         "integer", "Location", "Floor"),
    ("bed-natural-light", "boolean", "Location", "Natural light"),
    ("bed-feature",       "string",  "Location", "Bed feature (repeatable)"),
]

# datetime extensions are emitted as valueString (raw, lossless) but the value is
# an ISO-8601 timestamp; we type them as dateTime so the definition is meaningful.
# The builders use valueString, so type the SD's value[x] to match what's emitted.
_EMITTED_AS_STRING = {"dateTime"}  # treat dateTime specs as string on the wire


def _pascal(suffix: str) -> str:
    return "".join(p.capitalize() for p in suffix.split("-"))


def _build(suffix: str, type_code: str, context: str, title: str) -> StructureDefinition:
    url = X.EXT_BASE + suffix
    wire_type = "string" if type_code in _EMITTED_AS_STRING else type_code
    return StructureDefinition(
        id=suffix,
        url=url,
        name=_pascal(suffix),
        title=f"Hospilot {title}",
        status="active",
        fhirVersion=FHIR_VERSION,
        kind="complex-type",
        abstract=False,
        type="Extension",
        baseDefinition=EXTENSION_BASE,
        derivation="constraint",
        context=[{"type": "element", "expression": context}],
        differential={"element": [
            {"id": "Extension", "path": "Extension", "short": title,
             "definition": f"Hospilot extension -- {title}."},
            {"id": "Extension.extension", "path": "Extension.extension", "max": "0"},
            {"id": "Extension.url", "path": "Extension.url", "fixedUri": url},
            {"id": "Extension.value[x]", "path": "Extension.value[x]", "min": 1,
             "type": [{"code": wire_type}]},
        ]},
    )


STRUCTURE_DEFINITIONS: dict[str, StructureDefinition] = {
    suffix: _build(suffix, t, ctx, title) for (suffix, t, ctx, title) in _EXT_SPECS
}

# Canonical URLs covered, for the drift test.
DEFINED_URLS: set[str] = {X.EXT_BASE + s for s in STRUCTURE_DEFINITIONS}


def get(rid: str) -> StructureDefinition | None:
    return STRUCTURE_DEFINITIONS.get(rid)


def all_structure_definitions() -> list[StructureDefinition]:
    return list(STRUCTURE_DEFINITIONS.values())
