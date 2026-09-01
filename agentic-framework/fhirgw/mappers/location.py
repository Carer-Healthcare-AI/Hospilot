"""bed <-> FHIR Location.

Hospilot enrichment columns ride as extensions. The raw operational status
string (Available / reserved / Dirty / ...) is preserved in an extension so
to_internal reproduces the exact value agents compare against.
"""

from fhir.resources.location import Location

from fhirgw import terminology as T, extensions as X, identifiers as ID, narrative as N

# Operational columns accepted by the hospilot_beds upsert (no enrichment).
OPERATIONAL_KEYS = ("id", "branch_id", "ward", "bed_number", "room_type", "status", "is_active")

_STR_ENRICHMENT = {
    "ventilation": X.EXT_BED_VENTILATION,
    "room_sharing": X.EXT_BED_ROOM_SHARING,
    "wing": X.EXT_BED_WING,
    "noise_level": X.EXT_BED_NOISE_LEVEL,
}


def to_fhir(bed: dict) -> Location:
    status = bed.get("status")
    is_active = bed.get("is_active", True)

    exts: list[dict] = []
    if bed.get("ward") is not None:
        exts.append(X.ext_string(X.EXT_BED_WARD, bed["ward"]))
    if bed.get("room_type") is not None:
        exts.append(X.ext_string(X.EXT_BED_ROOM_TYPE, bed["room_type"]))
    if bed.get("branch_id") is not None:
        exts.append(X.ext_string(X.EXT_BED_BRANCH_ID, bed["branch_id"]))
    if status is not None:
        exts.append(X.ext_string(X.EXT_BED_RAW_STATUS, status))
    for key, url in _STR_ENRICHMENT.items():
        if bed.get(key) is not None:
            exts.append(X.ext_string(url, bed[key]))
    if bed.get("proximity") is not None:
        X.append_ext(exts, X.ext_int(X.EXT_BED_PROXIMITY, bed["proximity"]))
    if bed.get("floor") is not None:
        X.append_ext(exts, X.ext_int(X.EXT_BED_FLOOR, bed["floor"]))
    if bed.get("natural_light") is not None:
        exts.append(X.ext_bool(X.EXT_BED_NATURAL_LIGHT, bed["natural_light"]))
    for feat in (bed.get("features") or []):
        exts.append(X.ext_string(X.EXT_BED_FEATURE, feat))

    _ward = bed.get("ward")
    _summary = f"Bed {bed.get('bed_number') or bed['id']}" + (f" -- {_ward}" if _ward else "") + (f" ({status})" if status else "")
    kwargs: dict = {
        "id": str(bed["id"]),
        "identifier": [ID.identifier(ID.sys_bed(), bed["id"])],
        "status": T.location_status(is_active, status),
        "mode": "instance",
        "form": {"coding": [{"system": T.SYS_LOCATION_PHYSICAL, "code": "bd", "display": "Bed"}]},
        "text": N.text(_summary),
    }
    if bed.get("bed_number") is not None:
        kwargs["name"] = str(bed["bed_number"])
    op = T.operational_status(status)
    if op:
        kwargs["operationalStatus"] = {"system": T.SYS_LOCATION_OPER_STATUS, "code": op[0], "display": op[1]}
    if exts:
        kwargs["extension"] = exts
    return Location(**kwargs)


def to_internal(loc: Location) -> dict:
    ext = loc.extension
    return {
        "id": loc.id,
        "ward": X.get_ext(ext, X.EXT_BED_WARD),
        "bed_number": loc.name,
        "room_type": X.get_ext(ext, X.EXT_BED_ROOM_TYPE),
        "status": X.get_ext(ext, X.EXT_BED_RAW_STATUS),
        "is_active": loc.status != "inactive",
        "branch_id": X.get_ext(ext, X.EXT_BED_BRANCH_ID),
        "ventilation": X.get_ext(ext, X.EXT_BED_VENTILATION),
        "room_sharing": X.get_ext(ext, X.EXT_BED_ROOM_SHARING),
        "proximity": X.get_ext(ext, X.EXT_BED_PROXIMITY),
        "floor": X.get_ext(ext, X.EXT_BED_FLOOR),
        "wing": X.get_ext(ext, X.EXT_BED_WING),
        "natural_light": X.get_ext(ext, X.EXT_BED_NATURAL_LIGHT),
        "noise_level": X.get_ext(ext, X.EXT_BED_NOISE_LEVEL),
        "features": X.get_ext_all(ext, X.EXT_BED_FEATURE),
    }


def to_upsert_row(loc: Location) -> dict:
    """Operational columns only -- what hospilot_beds upsert should write."""
    full = to_internal(loc)
    return {k: full.get(k) for k in OPERATIONAL_KEYS}
