# Flow tests — whole pipelines, end to end

`tests/e2e` exercises individual task activities. This directory exercises the
**orchestration**: six complete pipelines driven through the real graph runner,
asserting on which agents ran, in what order, and whether the level barrier held.

## Layout

| File | What it is |
|---|---|
| `_flows.py` | The flow catalog — six pipeline dicts, defined once |
| `_driver.py` | `run_flow()`: builds the session graph and drives it to a terminal state |
| `test_flow_coverage.py` | Static checks on the catalog. **No stack needed** |
| `test_flows_live.py` | The live end-to-end runs. Needs the stack |
| `conftest.py` | Env setup, the service gate, and the session fixture |

## The six flows

| Flow | Agents | Shape |
|---|---|---|
| `er_admission` | ambulance → er → (bed, staff) | fan-out |
| `icu_escalation` | icu → (bed, staff) | fan-out |
| `surgical` | ot → (staff, pharmacy, bed) | wide fan-out |
| `discharge_billing` | discharge → (billing, bed), billing → revenue | **fan-in, 3 levels** |
| `diagnostics` | lab → (pharmacy, er) | fan-out |
| `all_agents` | all 11 plannable agents | 4 levels, full width |

The five themed flows cover every plannable agent between them; `all_agents`
uses them all in one pipeline. `test_themed_flows_cover_every_plannable_agent`
enforces that — **add an agent to the registry without adding it to a flow and
CI fails**, which is the point.

`patient_verification_agent` is the one deliberate exclusion: it is planner-
injected and parks on a HITL interrupt, so it does not belong in a straight-
through flow.

## Running

```bash
# static catalog checks — no services, runs in ordinary CI
pytest tests/flows

# the live flows — needs the stack up
docker compose up -d
pytest tests/flows -m live -v
```

Without `-m live` the flow tests still **collect** and then skip, so a typo in a
flow definition is caught as a collection error in normal CI rather than hiding
until someone runs the stack.

If the marker is requested but a service is unreachable, the directory skips with
a message naming what was missing — an un-runnable test, not a failure.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `E2E_FABRIC_BASE_URL` | `http://localhost:8002` | Fabric on the host (compose maps 8002:8001) |
| `FLOW_TIMEOUT_SECONDS` | `300` | Per-flow budget; raise for a cold stack |

## What is and isn't asserted

**Asserted:** every declared agent is reached; no fatal; each running agent
leaves a non-empty result under its own key; for every edge the source runs in an
earlier superstep than the target; themed flows keep at least two levels; the
all-agent run doesn't collide results or cascade-skip everything.

**Not asserted:** the actual numbers. Those depend on whatever the stack is
seeded with, and pinning them would make this a data test that breaks whenever
the fixtures change. Output is checked for shape only.

## Notes

- `run_flow()` drives `graph.astream` rather than `runner.start_session()`,
  which is fire-and-forget. Driving the stream gives an explicit terminal state,
  a real timeout, and the per-superstep order — the thing most of these tests are
  about. Everything below the graph is untouched and fully live.
- A flow that parks on a HITL interrupt raises `FlowInterrupted`. A straight-
  through flow that suddenly parks is a real finding, not something to wait out.
- Session rows are left behind deliberately (there is no `delete_session`, and
  the rows are the record of the run). They are tagged `goal="flow-e2e"`.
