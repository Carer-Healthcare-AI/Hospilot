# Hospilot Fabric

Fabric is the data layer between a hospital's existing information system and Hospilot's
agents. It reads the hospital's APIs, maps everything to **FHIR R5**, and gives the Hospilot
backend one stable shape to consume — so integrating a different HIS means changing Fabric,
not the agents.

Fabric **owns no data**. It has no database and no cache. Every read goes upstream to the
hospital; everything it emits is derived.

---

## The three parties

The thing to understand first: Fabric sits between two systems, and each leg has its own
protocol. Arrows below show which way data moves; the label says who initiates.

```
     HOSPITAL HIS                       FABRIC                    HOSPILOT BACKEND
    (owns the data)                 (this service)                 (agents + internal DB)
          │                               │                               │
          │        ══ READ — one of three, per INTEGRATION_MODE ══        │
          │                               │                               │
          │ ─────────────────────────────►│                               │
          │   change_api  Fabric GETs $changed-resources                  │
          │ ─────────────────────────────►│                               │
          │   polling     Fabric GETs each resource API, diffs fields     │
          │ ─────────────────────────────►│                               │
          │   kafka       the HIS pushes  hospilot.changes.*              │
          │                               │──────────────────────────────►│
          │                               │   hospilot.data.{entity}      │
          │                               │                               │
          │        ══ WRITE — one of two, per INTEGRATION_MODE ══         │
          │                               │                               │
          │ ◄─────────────────────────────│                               │
          │   change_api   the HIS GETs $pending-changes, then POSTs      │
          │   + polling    $acknowledge and $confirm                      │
          │ ◄─────────────────────────────│                               │
          │   kafka        Fabric pushes  hospilot.sync.write             │
          │                               │──────────────────────────────►│
          │                               │   hospilot.sync.ack           │
          │                               │                               │
          │                               │◄──────────────────────────────│
          │                               │   agents query live, HTTP     │
          └───── HTTP/FHIR, except ───────┴──────── Kafka, except ────────┘
              kafka mode's two topics             the agents' HTTP queries
```

Read that bottom line carefully, because the obvious summary is wrong. It is **not** true
that the hospital never touches Kafka:

- In `change_api` and `polling` mode it doesn't — the hospital speaks only HTTP/FHIR, and
  Kafka is purely internal to Hospilot.
- In `kafka` mode the hospital speaks Kafka on both legs: it produces to
  `hospilot.changes.*` and consumes `hospilot.sync.write`.

Either way Fabric **never connects to the backend's internal DB**. The backend consumes
Fabric's topics and caches them; when this codebase says an entity is "cached", it means
the *backend* caches it, never that Fabric wrote it there.

---

## How data gets in: `INTEGRATION_MODE`

Hospitals differ in what they can offer, so ingest has three implementations. Exactly one
runs, chosen by `INTEGRATION_MODE`, and all three end by publishing to Kafka.

**`INTEGRATION_MODE` sets both directions**, which is easy to miss — the read leg is what
the name describes, but it also picks how writes get out:

| Mode | Read: how Fabric learns of changes | Write: how queued changes reach the HIS |
|---|---|---|
| `change_api` *(default)* | Polls the HIS's `$changed-resources` FHIR feed — `ingest/change_poller.py` | The **HIS pulls** `$pending-changes` over HTTP — `writeback/http/` |
| `polling` | No change feed upstream, so Fabric polls each resource API and diffs field by field — `ingest/diff_poller.py` | Identical to `change_api` — the **HIS pulls** over HTTP |
| `kafka` | The HIS pushes to `hospilot.changes.*`; Fabric consumes — `ingest/kafka_consumer.py` | **Fabric pushes** to `hospilot.sync.write` — `writeback/kafka/` |

So the write leg is really a binary on `kafka` mode, not a three-way split: `polling`
writes exactly like `change_api`. Worth noting for integrators — `polling` exists because
a HIS *can't offer* a change feed, yet its write leg still asks that HIS to implement the
three-step pull handshake below.

Ingest only starts when `KAFKA_BOOTSTRAP_SERVERS` is set. Without a broker Fabric still
serves its REST API normally — you just get no change stream, which is the usual local-dev
setup.

## How data gets out: the write handshake

Agents don't write to the hospital directly. A write becomes a queued `PendingChange`, and
the HIS collects it in three steps:

1. `GET /fhir/Bundle/$pending-changes` — mints a `snapshot_id`, returns the queue as a FHIR
   R5 transaction Bundle. Re-pulling returns the **same** snapshot, not a new one.
2. `POST /fhir/Bundle/$pending-changes/$acknowledge` — the HIS confirms durable receipt;
   Fabric holds a soft lock. The queue is not cleared yet.
3. `POST /fhir/Bundle/$pending-changes/$confirm` — the HIS reports accepted/rejected per
   change. Fabric publishes one ack per change to `hospilot.sync.ack` and releases the lock.

Miss step 3 for `SNAPSHOT_LOCK_TIMEOUT_MS` (default 60s) and the lock expires and those
changes are re-offered — delivery is at-least-once, and the HIS should dedupe on
`change_id`. Under `INTEGRATION_MODE=kafka` these routes return **409** instead: proposals
are pushed to `hospilot.sync.write` by `writeback/`, and the two must not race the queue.

---

## Two ways data reaches an agent

Everything an agent reads arrives by exactly one of two routes. Which one depends on **what
is being asked for**, not on the entity:

> **Kafka carries the state of a record. The runtime API answers a question about many
> records.** So `bed` is a Kafka topic and `/beds/dirty` is an API route — same subject,
> different unit, no overlap.

### 1. Over Kafka → the backend's internal DB

Fabric publishes these as `hospilot.data.{entity}`. The backend caches each record and the
agents read it from there, so Fabric is not in the request path at all.

`bed` · `admission` · `discharge_ready` · `visit` · `task` · `lab_order` · `lab_result` ·
`lab_sample` · `lab_analyzer` · `pharmacy_order` · `pharmacy_inventory` · `ot_room` ·
`ot_room_status` · `ot_schedule` · `ot_surgery` · `ambulance` · `appointment` ·
`doctor_slot` · `ventilator` · `staff` · `staff_roster`

For eight of them — `ot_room`, `ot_room_status`, `ot_schedule`, `ot_surgery`, `ambulance`,
`ventilator`, `staff`, `staff_roster` — this is the **only** way to read them: Fabric exposes
no GET route for any of the eight. (`ot_schedule` has one *write* route,
`POST /ot/surgery-schedule/{id}/reschedule`, which queues a change rather than serving data.)

`ventilator`, `staff` and `staff_roster` are wired but **inert**: the topics stay empty until
the hospital exposes `/api/sync/{ventilator,staff,staff_roster}`.

### 2. Over the runtime API → agents call Fabric

Everything a per-record cache lookup can't answer. Three kinds of question end up here:

| Kind | Examples |
|---|---|
| **Filtered subsets** of a streamed entity | `/beds/dirty-icu`, `/beds/postop`, `/admissions/discharge-eligible`, `/visits/untriaged`, `/tasks/overdue`, `/pharmacy/orders/stat`, `/appointments?provider_id=…` |
| **Computed aggregates** | `/beds/summary`, `/er/pressure`, `/admissions/discharge-horizon`, `/admissions/discharge-ready-count`, `/tasks/completed-count` |
| **Never-cached data** — no topic exists | all 13 `financial/*`; the lab and pharmacy rules and log tables (`/labs/qc-logs`, `/labs/reflex-rules`, `/pharmacy/interactions`, `/pharmacy/controlled-log`, …); `/ot/equipment-usage`; `/departments`; `/patients*` (⚠ PHI, deliberately never cached) |

Rule of thumb for anything new: if the agent can already name the record it wants, it comes
from Kafka; if it is asking Fabric to *find* records, it's an API route.

---

## PHI

Fabric is pseudonymous nearly everywhere. Records carry an opaque `patient_token`; there is
no patient table here and no PHI at rest.

The exception is `runtime/patients.py`, backed by `service/transform.py::patient()`,
which resolves a token to real demographics (name, mobile, UHID) for `/patients`,
`/patients/{token}` and `/patients/by-mobile`. Treat that module as the PHI boundary:

- **never** run with `FABRIC_API_KEY` unset where those routes are reachable
- don't log their responses
- `/patients/by-mobile` is a reverse lookup — unauthenticated, it would let a caller
  enumerate patients by phone number

---

## Layout

One folder per direction of data flow, with transports as subfolders inside it:

```
src/
├── runtime/       hospilot's agents query this continuously — one module per domain
│                    beds, admissions, vitals, visits, nursing_tasks, labs,
│                    pharmacy, financial, patients (⚠ PHI), departments, ot,
│                    appointments
│
├── ingest/        HIS → Fabric.  One module per INTEGRATION_MODE, one runs.
│                    change_poller (change_api) · diff_poller (polling)
│                    kafka_consumer (kafka) · topic_map (entity → topic registry)
│
├── writeback/     Fabric → HIS.  The whole write leg.
│   │                proposals (translate) · change_store (queue) · bundle (FHIR)
│   ├── http/      change_api + polling: the HIS PULLS $pending-changes
│   └── kafka/     kafka mode: Fabric PUSHES to hospilot.sync.write
│
├── messaging/     → hospilot-backend over Kafka.  Nothing HIS-facing.
│                    producer (shared connection) · data_events (topics + payloads)
│
├── initial_sync/  One-time bulk dumps so the backend can seed its internal DB.
│                    api (endpoints) · registry (which tables are syncable)
│
├── service/       Upstream reads + transforms.  Owns no data, no writes.
├── clients/       HTTP out to the three upstream APIs (fhir, rest, sync)
├── fhirgw/        FHIR R5 vocabulary + mappers.  No I/O.
├── config.py      all settings (see .env.example)
└── main.py        app + lifespan: mounts 3 routers, starts 1 ingest task
```

Two naming notes that save confusion:

- **`writeback/http/` is not the `change_api` mode.** The mode is a *read* strategy
  (`ingest/change_poller.py`); `writeback/http/` is the *write* exit. Both got called
  "change" historically — the HIS's changes versus Hospilot's pending changes.
- **`fhirgw`, not `fhir`** — `src/` is the import root, so a package named `fhir` would
  shadow the `fhir.resources` library.

Each package's `__init__.py` documents its own scope and dependencies. Start with
`writeback/__init__.py`, which diagrams the full write pipeline, and `service/__init__.py`.

---

## Running it locally

Requires Python 3.11+ (CI runs 3.12).

```bash
cd fabric
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # requirements.txt + pytest
cp .env.example .env                     # defaults work against a local HIS on :3001

python -m pytest                         # 72 tests, no network or broker needed
uvicorn main:app --app-dir src --port 8001
```

Then open `http://localhost:8001/docs` — every route carries a summary, so the generated
page is the API reference. `GET /health` needs no auth.

With the whole stack, including a single-node Kafka:

```bash
docker compose -f deployments/docker-compose.fabric.yml up --build
```

### Configuration notes

Everything comes from the environment or `.env`; see `.env.example` for the full annotated
set and `src/config.py` for the defaults. Two that catch people out:

- **`FINANCIAL_API_BASE_URL` is load-bearing beyond finance.** The OT / ambulance /
  appointment REST base and the initial-sync base are both *derived* from it, so a wrong
  value silently breaks those too.
- **`FABRIC_API_KEY` blank disables Fabric's own auth.** Fine locally, not anywhere shared —
  see the PHI section.

---

## Tests

```bash
python -m pytest                  # all 72
python -m pytest tests/test_transform.py -v
```

Six suites covering FHIR mapping round-trips, the transform layer, endpoint wiring, the
polling-mode differ, the two-phase pending-changes protocol, and kafka-mode payload
handling. All hermetic: upstreams are mocked, and no Kafka broker or database is required.
