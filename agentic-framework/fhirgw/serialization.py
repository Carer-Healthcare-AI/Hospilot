"""
Serialize fhir.resources models into HTTP responses with the correct FHIR media
type. We pre-serialize to a JSON string and hand Starlette raw bytes so FastAPI
never re-encodes the model (which would emit application/json) and we never
double-encode a JSON string via JSONResponse.

Outbound responses use the "public" view: the Hospilot-proprietary extensions
(EXT_BASE) are stripped so external consumers / validators get clean, standard
FHIR with no Implementation Guide to load. The internal FHIR mirror (poller
dual-write) keeps the extensions for lossless round-trip -- it serializes models
directly, not via this module.
"""

import json

from starlette.responses import Response

from fhirgw.extensions import EXT_BASE

FHIR_MEDIA_TYPE = "application/fhir+json"


def fhir_json(resource) -> str:
    """Canonical FHIR JSON string: FHIR field names (aliases), no null fields."""
    return resource.model_dump_json(exclude_none=True, by_alias=True)


def _strip_hospilot_extensions(node):
    """Recursively drop any extension whose url is a Hospilot canonical (EXT_BASE).

    Standard FHIR extensions (none today) are preserved; this only removes the
    `https://hospilot.carer.ai/...` enrichment/round-trip extensions.
    """
    if isinstance(node, dict):
        ext = node.get("extension")
        if isinstance(ext, list):
            kept = [e for e in ext if not str(e.get("url", "")).startswith(EXT_BASE)]
            if kept:
                node["extension"] = kept
            else:
                node.pop("extension", None)
        for value in node.values():
            _strip_hospilot_extensions(value)
    elif isinstance(node, list):
        for item in node:
            _strip_hospilot_extensions(item)
    return node


def fhir_json_public(resource) -> str:
    """Outbound JSON: canonical FHIR with Hospilot-proprietary extensions removed."""
    data = json.loads(fhir_json(resource))
    _strip_hospilot_extensions(data)
    return json.dumps(data, separators=(",", ":"))


def fhir_response(resource, status_code: int = 200, headers: dict | None = None,
                  public: bool = False) -> Response:
    content = fhir_json_public(resource) if public else fhir_json(resource)
    return Response(
        content=content,
        media_type=FHIR_MEDIA_TYPE,
        status_code=status_code,
        headers=headers,
    )
