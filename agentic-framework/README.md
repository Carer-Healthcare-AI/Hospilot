# Hospilot Agentic Framework

The backend that runs Hospilot's agents. A user states a goal in natural language; an LLM
**planner** turns it into a pipeline of hospital agents (bed, ER, ICU, revenue, staffing),
runs that pipeline as a single **LangGraph** state machine, pauses for human approval where
needed, and streams results back over WebSocket.

It reads and writes all hospital data through [Fabric](../fabric) — this service owns no
clinical data of its own. Its own stores hold sessions, approvals, the agent/task registry, and durable checkpoints.

---

## The execution model

One session = one goal = one graph. The path from a prompt to an answer:

```
  user goal
     │
     ▼
  PLANNER (LLM)                 workflows/planner.py
     │   picks which agents are needed and wires them into a DAG
     ▼
  PIPELINE  →  LangGraph StateGraph          workflows/graph/builder.py
     │   one node per agent, levelled so a whole level runs in one superstep
     ▼
  AGENT node                    workflows/graph/agents/*, agents/<domain>/
     │   selects sub-agents, each sub-agent runs one or more TASKS
     ▼
  TASK                          agents/_shared/*, workflows/unified_executor.py
     │   builtin, or LLM-generated from a data *schema* and run sandboxed
     ▼
  SYNTHESIS  →  answer + proposed writes (queued through Fabric)
```

Four ideas do most of the work:

- **Planner → pipeline.** `workflows/planner.py` asks the LLM which of the registered agents
  a goal needs and how they depend on each other. `workflows/graph/builder.py` compiles that
  into a `StateGraph`, connecting consecutive levels fully so each level acts as a barrier.
- **Agent → sub-agent → task.** An agent node picks sub-agents; each runs tasks. Tasks are
  either **builtin** (`agents/_shared/builtin_tasks.py`) or **dynamically generated**:
  `workflows/unified_executor.py` asks the LLM for code from *field names and types only* —
  patient data never goes to the model — then runs it in a `RestrictedPython` sandbox and
  caches it in `task_registry`.
- **Human-in-the-loop.** Approvals are a LangGraph `interrupt()`, not a side channel. The
  graph pauses, the pending step lands on an approval queue, and the session resumes on
  decision. Assisted mode (human approves) is the default; the autonomous policy engine
  (`workflows/graph/policy.py`) ships but is off by default.
- **Durability.** The graph checkpoints to Postgres (`AsyncPostgresSaver`), so an
  approval-paused session survives a restart and resumes on the exact node. Redis holds
  live session, queue and HITL state.

---

## The agents

Five domain agents ship in this cut (`agents/`, declared in
`agents/_shared/manifest.py`):

| Agent | Does |
|---|---|
| `bed_agent` | Bed availability, status, ranking, reservations, dirty-bed recovery |
| `er_agent` | ER queue management, triage scoring, admission and fast-track routing |
| `icu_agent` | ICU census, ventilator tracking, step-down candidate identification |
| `revenue_agent` | Billing-gap & leakage review, profitability, denial-risk prediction |
| `staff_agent` | Nurse–patient ratios, float-pool availability, shift staffing levels |

Each entry in the **manifest** declares exactly what an agent may touch — its Redis keys,
Hasura tables, session-context fields, and tool schemas. The guardrail
(`agents/_shared/guardrail.py`) uses it twice: when a new dynamic task is written, and before
an unrecognised task runs. Add a manifest entry whenever an agent gains a new data source.

---

## Layout

```
agents/
├── _shared/       manifest (data contract) · guardrail · builtin & generic tasks
│                    fetch tools · task ranking · generated-task loader
├── bed/ er/ icu/  one folder per domain agent
│   revenue/ staff/

workflows/
├── planner.py         LLM goal → pipeline (agents + edges)
├── unified_executor.py  schema-only codegen + RestrictedPython sandbox
├── materializer.py · task_codegen.py · dynamic_task.py
├── graph/             the LangGraph runtime
│   ├── builder.py     pipeline snapshot → StateGraph
│   ├── runner.py      start / resume / cancel a session
│   ├── nodes.py conditions.py planning.py hitl.py policy.py
│   ├── observability.py  Postgres checkpointer + Langfuse
│   └── agents/        per-agent graph nodes + registry
└── system_prompts/    planner & agent prompts

api/routes/        FastAPI — sessions · approvals · queues · ws · agents
│                    auth · orgs · users   (multi-tenant, JWT)
messaging/         Kafka event/replay bus → WebSocket relay; Fabric data → Redis seed
db/                hasura (primary store + org routing) · fabric (data-layer client)
cache/             Redis
config.py          all settings (see .env.example)
main.py            FastAPI app + lifespan (Redis, checkpointer, Kafka, reaper)
```

Multi-tenant: each org routes to its own Hasura source; a platform `super_admin` is
bootstrapped on first boot. Provision tenants with `scripts/provision_org.py`.

---

## Running it locally

Requires Python 3.11+. Only `HASURA_URL` / `HASURA_ADMIN_SECRET` are needed to boot; without
`DATABASE_URL` the checkpointer runs in-memory (approvals are lost on restart), and Kafka /
Temporal / the policy engine are off by default.

```bash
cd agentic-framework
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # fill in HASURA_* and ANTHROPIC_API_KEY

python run.py                            # or: uvicorn main:app --port 8000
```

Then open `http://localhost:8000/docs`. `GET /health` needs no auth.

### Configuration notes

Every var in `.env.example` maps 1:1 to a field in `config.py`. Worth flagging:

- **`LLM_PROVIDER`** — `anthropic` (needs `ANTHROPIC_API_KEY`) or `ollama` (an
  OpenAI-compatible local endpoint via `LLM_BASE_URL`).
- **`FABRIC_BASE_URL` unset** → agent data reads fail. Point it at a running Fabric.
- **`DATABASE_URL` blank** → in-memory checkpointer; fine for dev, loses paused sessions.
- **`JWT_SECRET`** and **`BOOTSTRAP_ADMIN_*`** — set these before anything shared.

---

## Tests

```bash
python -m pytest
```
