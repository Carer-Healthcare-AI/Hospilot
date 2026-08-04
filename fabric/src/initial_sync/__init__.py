"""Initial sync — one-time bulk table dumps to seed hospilot-backend's cache.

Used once per deployment (or after a cache wipe) so the backend can populate its internal
DB from scratch, before the Kafka change feed takes over for incremental updates.
Keyset-paginated, because these are whole-table reads.

  api.py       the endpoints: GET /sync/tables, GET /sync/{table}
  registry.py  which logical tables are syncable, and their upstream SQL sources

Distinct from runtime/ in caller and cadence: the backend drains these at startup and
then stops, whereas runtime routes serve agents continuously.

The keyset paging itself is NOT here — it's clients/sync_client.py, because ingest
needs it too (diff_poller walks /sync/lab_result; service/{staff,ventilator} source
entities the HIS exposes nowhere else). This package is only the initial-sync API.
"""

from initial_sync.api import router

__all__ = ["router"]
