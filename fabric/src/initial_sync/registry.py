"""Which tables are syncable, and a one-page pass-through to fetch them.

Thin layer over the DB's keyset-paginated /api/sync/<table> endpoints.

This is used ONCE by the main backend to populate the backend's internal DB from scratch with a
full dump of each table, before the Kafka change-feed mechanism takes over for
incremental updates. Fabric does not transform these rows — it forwards the DB's
raw rows + pagination envelope so the backend can mirror tables verbatim.

The 14 syncable tables mirror the DB's sync registry. Keep this list in sync
with INITIAL_SYNC_API.md / the DB's syncPaginator.js registry.
"""

from clients import sync_client

# logical :table value -> source SQL table (for docs/observability; the DB owns
# the authoritative registry). Order matches INITIAL_SYNC_API.md.
TABLE_SOURCES: dict[str, str] = {
    "admission": "hospilot.ipd_admissions",
    "ambulance": "hospilot.emergency_ambulances",
    "bed": "hospilot.beds",
    "dept": "hospilot.departments",
    "discharge_ready": "hospilot.ipd_admissions (discharge_ready = TRUE)",
    "discharge_summary": "hospilot.discharge_summaries",
    "lab": "hospilot.lab_orders",
    "lab_result": "hospilot.lab_results",
    "lab_sample": "hospilot.lab_samples",
    "lab_analyzer": "hospilot.lab_analyzers",
    "ot_room": "hospilot.ot_rooms",
    "ot_room_status": "hospilot.ot_room_status",
    "ot_schedule": "hospilot.ot_surgery_schedule",
    "ot_surgery": "hospilot.ot_surgeries",
    "pharmacy_order": "hospilot.pharmacy_orders",
    "pharmacy_inventory": "hospilot.pharmacy_inventory",
    "task": "hospilot.nursing_tasks",
    "vital": "hospilot.vitals",
    # HRMS / ICU tables (DATA_NEEDED). These rows exist in Postgres but the DB's sync
    # registry must add /api/sync/<table> before they resolve — until then the forward
    # returns the DB's 404 and the Kafka pollers (which drain these same pages) publish
    # nothing. (ED has no table here — ER demand = EMER visits, already synced.)
    "staff": "hospilot.staff",
    "staff_roster": "hospilot.staff_roster",
    "ventilator": "hospilot.ventilator",
}

TABLES: list[str] = list(TABLE_SOURCES)


def is_valid_table(table: str) -> bool:
    return table in TABLE_SOURCES


async def page(
    table: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    sync_id: str | None = None,
) -> dict:
    """Fetch one keyset page for `table`, forwarding the DB's envelope as-is.

    Deliberately one page, not all of them: the backend walks the cursor itself so it can
    checkpoint between pages. Callers that DO want every row (service/staff, ventilator)
    use clients.sync_client.fetch_all instead.
    """
    return await sync_client.fetch_page(
        table, limit=limit, cursor=cursor, sync_id=sync_id
    )
