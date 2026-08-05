"""department <-> FHIR Organization."""

from fhir.resources.organization import Organization

from fhirgw import extensions as X, identifiers as ID, narrative as N

OPERATIONAL_KEYS = ("id", "name", "type")


def to_fhir(dept: dict) -> Organization:
    raw_type = dept.get("type")
    name = dept.get("name")

    kwargs: dict = {
        "id": str(dept["id"]),
        "identifier": [ID.identifier(ID.sys_department(), dept["id"])],
        "text": N.text(str(name) if name else f"Organization {dept['id']}"),
    }
    if name:  # FHIR `string` cannot be empty
        kwargs["name"] = str(name)
    if raw_type is not None:
        kwargs["type"] = [{"text": str(raw_type)}]
        kwargs["extension"] = [X.ext_string(X.EXT_ORG_RAW_TYPE, raw_type)]
    return Organization(**kwargs)


def source_organization() -> Organization:
    """The recording system/facility, referenced as Observation.performer when no
    individual practitioner is captured. Served by the read route so the
    performer reference resolves."""
    oid = ID.source_organization_id()
    return Organization(
        id=oid,
        identifier=[ID.identifier(ID.sys_organization(), oid)],
        name="Recording system (EHR source)",
        text=N.text("Recording system (EHR source)"),
    )


def to_internal(org: Organization) -> dict:
    return {
        "id": org.id,
        "name": org.name if org.name is not None else "",
        "type": X.get_ext(org.extension, X.EXT_ORG_RAW_TYPE),
    }


def to_upsert_row(org: Organization) -> dict:
    return to_internal(org)
