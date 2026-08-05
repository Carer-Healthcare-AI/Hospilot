"""
Identifier strategy.

- Logical `.id` on each resource is the internal UUID (URL-safe, stable) so
  `GET /fhir/Location/{uuid}` is a direct cache lookup.
- `.identifier` mirrors it with a Hospilot `system` URI so external systems have
  a namespaced business identifier.
- Patient is PSEUDONYMOUS: its ONLY identifier is the `patient_token`; no name,
  birthDate, gender, or other PHI is ever attached (PHI lives in CarerOS).

Identifier systems are derived from settings.fhir_base_url so they are stable
per-deployment. Distinct systems for admission-vs-visit and vital-vs-lab let a
future write-back route a resource to the correct Hospilot table.
"""

from config import settings


def _base() -> str:
    return settings.fhir_base_url.rstrip("/")


def sys_patient_token() -> str: return f"{_base()}/identifier/patient-token"
def sys_admission() -> str:     return f"{_base()}/identifier/admission"
def sys_visit() -> str:         return f"{_base()}/identifier/visit"
def sys_bed() -> str:           return f"{_base()}/identifier/bed"
def sys_department() -> str:    return f"{_base()}/identifier/department"
def sys_organization() -> str:  return f"{_base()}/identifier/organization"
def sys_vital() -> str:         return f"{_base()}/identifier/vital"
def sys_lab() -> str:           return f"{_base()}/identifier/lab-result"
def sys_lab_code() -> str:      return f"{_base()}/CodeSystem/lab-test-code"


# --- source facility (Observation.performer when no individual is recorded) ----
def source_organization_id() -> str:
    """Stable id of the recording system/facility, derived from the EHR source."""
    return f"source-{settings.ehr_source}"


def source_organization_reference() -> dict:
    return reference("Organization", source_organization_id())


def identifier(system: str, value) -> dict:
    return {"system": system, "value": str(value)}


def patient_identifier(token) -> dict:
    return identifier(sys_patient_token(), token)


def reference(resource_type: str, rid) -> dict:
    """A FHIR relative reference, e.g. {'reference': 'Patient/<token>'}."""
    return {"reference": f"{resource_type}/{rid}"}


def patient_reference(token) -> dict:
    return reference("Patient", token)
