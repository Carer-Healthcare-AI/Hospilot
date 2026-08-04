"""Write-back to the HIS — the complete outbound write leg, in one place.

Hospilot never writes to the hospital directly. An agent's write becomes a queued
proposal, and leaves by whichever transport INTEGRATION_MODE selects:

    agent POSTs to runtime/{beds,vitals,visits,admissions,ot,appointments}.py
        │
        ▼
    proposals.py        translate to a PendingChange (approval flag, FHIR target)
        │
        ▼
    change_store.py     the queue — one in-flight snapshot at a time, soft-locked
        │
        ├── http/       change_api + polling: the HIS PULLS  ($pending-changes)
        └── kafka/      kafka mode:           Fabric PUSHES  (hospilot.sync.write)

Exactly one exit is live. `http/` returns 409 in kafka mode so the two can't race the
same queue; `kafka/` never starts otherwise. Both build the same FHIR R5 transaction
Bundle via bundle.py, so the HIS sees an identical payload either way.

Both are at-least-once and the HIS must dedupe on `change_id`: the HTTP path re-offers
a snapshot whose lock expired, the Kafka path re-offers anything it didn't confirm
sending.

Depends downward on messaging.producer (the shared Kafka connection) and fhirgw.*
(terminology, extensions, identifiers) for Bundle construction.
"""
