"""Staff + staff-roster reads (HRMS).

No FHIR resource and (today) no plain-REST list endpoint on the DB, so Fabric
sources these from the DB's keyset sync API — the same approach used for
lab_result. Rows pass through in the DB's shape (no transform). Inert until the
DB registers /api/sync/staff and /api/sync/staff_roster.
"""

from clients import sync_client


async def members() -> list[dict]:
    return await sync_client.fetch_all("staff")


async def roster() -> list[dict]:
    return await sync_client.fetch_all("staff_roster")
