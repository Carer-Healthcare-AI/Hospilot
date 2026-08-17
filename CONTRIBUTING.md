# Contributing to Hospilot

Thanks for looking into this — contributions of any size are welcome, from a typo fix to
a new domain agent.

## Local setup

Each service sets up independently:

- [`agentic-framework/README.md`](./agentic-framework/README.md#running-it-locally) —
  the planner + agent orchestration backend
- [`fabric/README.md`](./fabric/README.md#running-it-locally) — the HIS/FHIR data layer

Or bring up both together with Docker — see the root [`README.md`](./README.md#quick-start)
Quick Start.

## Before you open a PR

- **Run the tests.** `python -m pytest` in whichever service you touched. Fabric's suite
  (72 tests) is fully hermetic — no network or broker required. Add tests for new
  behavior; a PR that only adds code without covering it is a slower review, not a faster
  one.
- **Keep changes scoped.** A PR that fixes one thing is easier to review — and more likely
  to get merged quickly — than one that also reformats unrelated files or renames things
  in passing.
- **Follow the patterns already in the file you're editing**, not a different style you
  prefer. Consistency across the codebase matters more than any one file's local
  optimum.

## Adding a new agent

An agent is more than a Python module — it needs a **manifest entry**
(`agents/_shared/manifest.py`) declaring exactly what it's allowed to touch: Redis keys,
Hasura tables, session-context fields, tool schemas. The guardrail
(`agents/_shared/guardrail.py`) enforces this at runtime, so a new agent without a manifest
entry won't be trusted with dynamic task generation. Read `housekeeping_agent` first, not
`bed_agent` — `bed_agent` is the reference port and the most complex agent in the catalog
(489 lines), while `housekeeping_agent`'s body is six lines and shows the same
body-function + activities + manifest-entry shape without the noise. See
[`INTEGRATIONS.md`](./INTEGRATIONS.md#langgraph--writing-a-new-agent) for the excerpt.

## Adding a new HIS integration

Everything HIS-specific belongs in [Fabric](./fabric), not in the agents — agents only ever
talk to Fabric's stable, FHIR-shaped API. See `fabric/README.md`'s `INTEGRATION_MODE`
section for the three supported ingest strategies (`change_api`, `polling`, `kafka`) and
pick whichever matches what the target HIS can actually offer.
[`INTEGRATIONS.md`](./INTEGRATIONS.md#hishmis-ingest) has the config and the module each
mode dispatches to.

## Reporting bugs / proposing features

Open an [Issue](../../issues). For a bug, include: what you expected, what happened
instead, and how to reproduce it locally (which service, which endpoint/goal, relevant
config). For a feature or a new agent idea, a short description of the hospital operations
problem it solves is more useful upfront than an implementation sketch.

Issues labeled `good first issue` are scoped for a first-time contributor — small, with a
clear definition of done, and don't require deep familiarity with the planner or the
LangGraph runtime.

## Code of conduct

Be direct, be kind, assume good faith. Disagreements about code are normal; disagreements
about people aren't welcome here.
