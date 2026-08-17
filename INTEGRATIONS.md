# Integrations

Concrete, working entry points for wiring Hospilot into your stack — real endpoints, real
config, real code, each pointing at the file it comes from. This isn't a replacement for the
service READMEs; it's the "show me a snippet" version of the [Integrations
table](./README.md#integrations).

---

## FHIR — Fabric's REST API

Fabric exposes one route module per domain in `fabric/src/runtime/` (`beds.py`,
`patients.py`, `admissions.py`, `labs.py`, `ot.py`, `pharmacy.py`, `vitals.py`, and more),
mounted behind `require_fabric_auth` (bearer token or `x-api-key`). The generated Swagger UI
at `http://localhost:8001/docs` is the canonical reference — every route's `summary=` feeds
it directly, so it's always current.

**Reads** — `fabric/src/runtime/beds.py`:

```
GET /beds
GET /beds/available-icu
GET /beds/dirty
GET /beds/summary
GET /beds/{bed_id}
```

**Writes are queued, not direct** — `POST /beds/{bed_id}/status` takes `{"status": "..."}`
and returns `{"ok": true, "id": bed_id, "status": ...}`, but this queues a `PendingChange`
rather than writing upstream immediately. That queue is drained through a FHIR R5
transaction Bundle handshake (`fabric/src/writeback/http/pending_changes.py`):

```
GET  /fhir/Bundle/$pending-changes             → FHIR Bundle of queued writes
POST /fhir/Bundle/$pending-changes/$acknowledge  {"snapshot_id"}
POST /fhir/Bundle/$pending-changes/$confirm      {"snapshot_id", "results": [
                                                     {"change_id", "status", "reason?", "assigned_id?"}
                                                  ]}
```

Your HIS integration reads the Bundle, applies the changes on its side, then confirms —
Fabric never assumes a write succeeded until you tell it so.

---

## HIS/HMIS ingest

One env var picks the ingest strategy — `INTEGRATION_MODE` in `fabric/src/config.py`, one
of `change_api` (default), `polling`, or `kafka` (only active if `KAFKA_BOOTSTRAP_SERVERS`
is set). Each mode is a standalone module exposing `async def run()`, dispatched from
`fabric/src/main.py`:

| Mode | Module | Fit |
|---|---|---|
| `change_api` | `fabric/src/ingest/change_poller.py` | Your HIS exposes a "what changed since X" endpoint |
| `polling` | `fabric/src/ingest/diff_poller.py` | No change feed — Fabric diffs full snapshots on an interval |
| `kafka` | `fabric/src/ingest/kafka_consumer.py` | Your HIS already publishes change events |

There's no formal base class to implement yet — a new adapter is a new module shaped like
one of these three, wired into the same dispatch in `main.py`. If you build one for a HIS
we don't cover, that's exactly the kind of PR [CONTRIBUTING.md](./CONTRIBUTING.md) is asking
for.

---

## Kafka

Fabric uses `aiokafka`, not `confluent-kafka`. Producer setup
(`fabric/src/messaging/producer.py`):

```python
_producer = AIOKafkaProducer(
    bootstrap_servers=settings.kafka_bootstrap_servers,
    client_id=settings.kafka_client_id,
    acks="all",
    enable_idempotence=True,
)
await _producer.start()
```

**Topics** (prefix configurable via `KAFKA_TOPIC_PREFIX`, default `hospilot.data`):

- `hospilot.data.{entity}` — Fabric's outbound data events
- `hospilot.sync.ack` / `hospilot.sync.write` — ack and kafka-mode write channels
- `hospilot.changes.*` — inbound HIS change feed (17 topics, one per entity type)

**Message shape** (`fabric/src/messaging/data_events.py`):

```json
{"entity": "bed", "id": "...", "operation": "upsert", "data": {...}, "changed": [...]}
```

Consumer group is `hospilot-fabric-changes`, `auto_offset_reset="latest"` — Fabric doesn't
replay history on startup by default.

---

## LangGraph — writing a new agent

`bed_agent` (`agentic-framework/workflows/graph/agents/bed.py`) is the reference port but at
489 lines it's the *most* complex agent, not the simplest — don't start there. Start from
`housekeeping_agent` instead; the whole body is six lines
(`agentic-framework/workflows/graph/agents/simple.py`):

```python
async def run_housekeeping_body(sid: str, ctx: dict) -> dict:
    beds = await get_vacated_beds(sid)
    if not beds:
        return {"status": "completed", "message": "No beds currently require cleaning"}
    result = await dispatch_housekeeping(HousekeepingDispatchInput(session_id=sid, beds=beds))
    return {"status": "completed", "dispatched": result["dispatched"]}
```

backed by two Temporal activities in `agentic-framework/agents/housekeeping/activities.py`.
A new agent needs the same two pieces — a body function and its activities — plus a
manifest entry in `agentic-framework/agents/_shared/manifest.py` declaring what it's allowed
to touch:

```python
AgentDataSources(
    redis_keys=[...],
    hasura_tables=[...],
    context_fields=[...],
    description="...",
    tool_schemas=[...],
)
```

and a one-line registration in `agentic-framework/workflows/graph/agents/registry.py`
(`AGENT_BODIES["your_agent"] = run_your_body`). The guardrail
(`agents/_shared/guardrail.py`) enforces the manifest at runtime — an agent without one
won't be trusted with dynamic task generation.

---

## PostgreSQL (via Hasura)

Client is `HasuraClient` in `agentic-framework/db/hasura.py`, configured via `HASURA_URL` /
`HASURA_ADMIN_SECRET`. Real query excerpt:

```graphql
query RecentlyDischargedBeds {
  hospilot_ipd_admissions(
    where: {discharge_ready: {_eq: true}}
    order_by: {admitted_at: desc}
    limit: 20
  ) {
    id
    bed_id
    patient_token
    admitted_at
  }
}
```

Multi-tenant tables use a `{P}` prefix placeholder, resolved per-call from the request's
`org_id` — so the same query works across tenants without hardcoding a schema name.

---

## Redis

`agentic-framework/cache/redis.py`, configured via `REDIS_URL` (defaults to
`redis://localhost:6380`). Generic async helpers, all JSON-encoded under the hood:

```python
from cache import redis as cache

await cache.set(key, value, ttl=300)
value = await cache.get(key)
await cache.delete_pattern("session:*")
```

---

## Docker

Covered in the [Quick Start](./README.md#quick-start) — `deployments/` has a Dockerfile and
a Compose file per service (`agentic-framework.Dockerfile` / `fabric.Dockerfile`,
`docker-compose.agentic-framework.yml` / `docker-compose.fabric.yml`). No orchestration
beyond Compose exists yet.

## Kubernetes

Not provided. There are no manifests or Helm charts anywhere in this repo today — if you
need one, that's an open gap (tracked in the [Roadmap](./README.md#roadmap)), not a hidden
example. A PR adding one is welcome.
