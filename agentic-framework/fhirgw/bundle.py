"""
Assemble a FHIR `searchset` Bundle from a list of resources.
"""

from fhir.resources.bundle import Bundle, BundleEntry

from config import settings


def _resource_type(resource) -> str | None:
    rt = getattr(resource, "__resource_type__", None)
    if rt:
        return rt
    getter = getattr(resource, "get_resource_type", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _full_url(resource) -> str | None:
    rtype = _resource_type(resource)
    rid = getattr(resource, "id", None)
    if rtype and rid:
        return f"{settings.fhir_base_url.rstrip('/')}/{rtype}/{rid}"
    return None


def searchset(resources: list, self_url: str | None = None) -> Bundle:
    entries = []
    for r in resources:
        entry = BundleEntry(resource=r, search={"mode": "match"})
        fu = _full_url(r)
        if fu:
            entry.fullUrl = fu
        entries.append(entry)

    bundle = Bundle(type="searchset", total=len(resources))
    if entries:
        bundle.entry = entries
    if self_url:
        bundle.link = [{"relation": "self", "url": self_url}]
    return bundle
