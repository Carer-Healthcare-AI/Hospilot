"""Ventilator inventory reads (ICU).

No FHIR resource and no plain-REST list endpoint upstream, so Fabric sources this
from the DB's keyset sync API (like lab_result). Rows pass through in the DB's
shape. Inert until the DB registers /api/sync/ventilator.
"""

from clients import sync_client


async def units() -> list[dict]:
    return await sync_client.fetch_all("ventilator")
