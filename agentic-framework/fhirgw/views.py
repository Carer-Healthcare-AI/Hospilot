"""
Projection bridge: FHIR resource -> the internal dict agents consume.

The poller uses these to build the Redis projection from canonical FHIR, so the
agent-facing shape is *derived from FHIR* yet identical to today's keys. Anything
holding a FHIR resource can get the agent dict in one call.
"""

from fhirgw.mappers import location, organization, encounter, observation, patient

bed_view = location.to_internal
org_view = organization.to_internal
admission_view = encounter.admission_to_internal
visit_view = encounter.visit_to_internal
vitals_view = observation.vitals_to_internal
patient_view = patient.to_internal
