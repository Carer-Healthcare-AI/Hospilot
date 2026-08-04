"""Service layer — every read here goes UPSTREAM to the hospital's HIS.

Fabric owns no data and has no database. It is a client of three upstream APIs, and
which one a module uses is the only structural distinction in this package:

  clients.fhir_client   canonical FHIR R5 (clinical)   → clinical, lab
  clients.rest_client   plain REST (financial, OT,     → financial, ot, ambulance,
                        ambulance, appointments)          appointments, pharmacy, lab
  clients.sync_client   keyset bulk sync               → staff, ventilator (+ sync/, ingest/)

Pure transform, no upstream: transform.py — FHIR R5 resources → the normalized dicts
every route and poller returns.

Reads only. The write pipeline (queue, translation, the two exits) lives in writeback/;
appointments.py is the one module here that also queues writes, and it imports them
from there.

**Fabric never connects to the backend's internal DB.** Modules here mention the cache
often, but always to describe what hospilot-backend does after consuming Fabric's Kafka
events — the backend caches them, the agents read that cache. Fabric has no cache client, no such
dependency, and no cache of its own; "cached" means "the backend keeps it in its
internal DB", never "Fabric writes it there".

Two delivery paths lead to the agents, and most entities use both. See the table in
fabric/README.md, and each module's own docstring for its entities:
  • streamed     — published to Kafka, cached by the backend, read from the internal DB
  • pass-through — served live over Fabric's REST API, for the list / filter /
                   computed queries a per-record lookup cannot answer

PHI: only transform.patient() returns demographics (name, mobile, UHID), backing
/patients*. Everything else Fabric serves carries an opaque patient token only.
"""
