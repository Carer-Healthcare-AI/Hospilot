"""
Minimal pragmatic auth for the /fhir API: a shared bearer token / API key.

- settings.fhir_api_key empty  => auth disabled (matches the app's current
  public posture; main.py logs a warning at startup).
- otherwise every /fhir request must send `Authorization: Bearer <key>` or
  `X-API-Key: <key>`, EXCEPT GET /fhir/metadata which stays public for discovery.

Production path (not built here): SMART-on-FHIR / OAuth2 with scoped JWTs.
The data is pseudonymous (no direct PHI), which lowers but does not remove the
bar -- real deployments still want authn + rate limiting + audit.
"""

from fastapi import Request

from config import settings
from fhirgw.outcomes import security


async def require_fhir_auth(request: Request) -> None:
    key = settings.fhir_api_key
    if not key:
        return  # auth disabled

    if request.url.path.rstrip("/").endswith("/metadata"):
        return  # discovery is public

    auth = request.headers.get("authorization", "")
    provided = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    if not provided:
        provided = request.headers.get("x-api-key")

    if provided != key:
        raise security("Missing or invalid credentials for the FHIR API")
