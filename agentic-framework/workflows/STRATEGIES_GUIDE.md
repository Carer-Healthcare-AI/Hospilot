# Execution strategies

An **execution strategy** is the rule that decides how a group of agents behaves when they run
together. "Together" here means a **round** — a set of agents with no dependency between them,
so they can all fire at the same time, and the next round waits for this one to finish. (The
code calls a round a *level*, and older notes call it a *step* — all three words mean the same
one layer of agents running in parallel.)

Here's a flow with three levels (this is what the code and the rest of this guide call a
**round**). Each **box is one level**; every agent inside a box runs at the same time. The
arrows are dependencies — a level can't start until the box above it finishes.

```
┌─ level 1 ────────────────────────────────────────────────┐
│ ambulance_agent                                          │
└──────────────────────────────────────────────────────────┘
                              |
                              ▼
┌─ level 2 (bidding for bed resource) ─────────────────────┐
│ ICU_agent  ER_agent OT_agent                             │
└──────────────────────────────────────────────────────────┘
                              |
                              ▼
┌─ level 3 ────────────────────────────────────────────────┐
│ summary_agent                                            │
└──────────────────────────────────────────────────────────┘
```

`triage_agent` runs alone, then level 2's three agents all fire together (they share a box, so
nothing makes one wait for another), then `summary_agent` runs once they're all done.

The strategy is chosen **per round**, not once for the whole flow. So this request could run a
contest in level 2 (where `bed_agent_ward_A` and `bed_agent_ward_B` fight over one bed) and
plain "everyone works" collaboration in levels 1 and 3.

Two strategies exist today:

```json
{
  "common_goal": {
    "default": true,
    "does": "every agent runs; results are merged",
    "use_when": "agents are not competing for the same scarce thing"
  },
  "bidding": {
    "default": false,
    "does": "agents compete for one scarce unit — each returns an offer, highest wins and acts, the rest stand down",
    "use_when": "several agents want the same scarce resource and only one can have it",
    "handler": "rl_bidding"
  }
}
```

Which one gets picked is driven **entirely** by the `description` / `use when` text in
`workflows/strategies.json`. There is no selection rule baked into the prompt or the code — the
planner reads that text and matches it against the situation.

---

## The flow, stage by stage

The five stages form a pipeline: each one's output is the next one's input. Every stage below
shows the **shape of what it emits**, so you can see exactly what the next stage receives.

### 1. The planner is shown the menu

A request arrives. The planner (an LLM) designs the flow. Before it does, we hand it the list
of strategies with their plain-language `use when` lines and tell it to choose by matching that
text — nothing more.

Emits — the menu the planner now holds:

```json
{
  "menu": [
    { "id": "common_goal", "use_when": "not competing for one scarce thing" },
    { "id": "bidding",      "use_when": "several agents want the same scarce thing" }
  ]
}
```

Code: menu source `workflows/strategies.json`; turned into prompt text by
`strategy_catalogue_text()` in `strategies.py`; injected into the planner instructions in
`planner.py` (template `USER_TEMPLATE_AGENTS`).

### 2. The planner tags the agents that need a non-default strategy

The planner returns the flow as a list of agents. Each agent may carry an optional `strategy`
tag; an untagged agent inherits the default (`common_goal`). Only the agents whose situation
matches a strategy's `use when` get tagged — usually the small set contending for one resource.

Emits — the agent list, tagged where needed:

```json
{
  "agents": [
    { "name": "discharge_agent",  "strategy": null      },
    { "name": "bed_agent_ward_A", "strategy": "bidding" },
    { "name": "bed_agent_ward_B", "strategy": "bidding" }
  ]
}
```

An invalid or unknown tag is dropped and that agent falls back to the default, so a typo can
never break the flow.

Code: planning in `_plan_agents` (`planning_graph.py`) → `generate_agents_and_edges`
(`planner.py`); each tag validated and dropped-if-bad in `planner.py` via `is_valid_strategy`
(`strategies.py`).

### 3. Agents are grouped into rounds, and each round resolves to one strategy

The approved flow is compiled to run: agents are grouped into rounds, and every round resolves
to a **single** strategy from the tags it contains.

Emits — the rounds, each with one resolved strategy:

```json
{
  "rounds": [
    { "round": 1, "agents": ["discharge_agent"],                     "strategy": "common_goal" },
    { "round": 2, "agents": ["bed_agent_ward_A", "bed_agent_ward_B"], "strategy": "bidding"     }
  ]
}
```

Resolution rule:

- no tags in the round → default (`common_goal`);
- a `bidding` tag → the round is a bidding round.
- *(If more strategies are added later and one round carries two different tags, a fixed
  tie-break order decides, with a logged warning.)*

Code: grouping + tag carry-over in `_plan_levels` (`builder.py`); per-round resolution in
`_resolve_level_strategy` (`builder.py`).

### 4. The runnable flow is built

The rounds are assembled into the actual executable graph.

- A **default round** keeps its agents as separate nodes that run side by side.
- A **non-default round** is wrapped into a single node that runs the strategy inside itself.
  From the outside it looks like one node; the contest happens hidden within.

Rounds are then joined in order — a wrapped round takes the previous round's output as input and
feeds its own output to the next:

```
  round N  ──▶  [ wrapped round: agent 1 … agent n → one strategy ]  ──▶  round N+1
```

Emits — the runnable graph:

```json
{
  "nodes": [
    { "id": "round_1", "type": "default", "agents": ["discharge_agent"] },
    { "id": "round_2", "type": "wrapped", "strategy": "bidding",
      "agents": ["bed_agent_ward_A", "bed_agent_ward_B"] }
  ],
  "edges": [ { "from": "round_1", "to": "round_2" } ]
}
```

If no agent in the flow is tagged, no wrapping happens and the graph is built exactly as before
(the fast path); otherwise the mixed default-plus-wrapped path runs.

Code: `build_session_graph` (`builder.py`); a wrapped round is built by `_make_level_node`
(`builder.py`).

### 5. Execution

At run time each round executes under its resolved strategy.

A **default round** → handler `common_goal` (`strategies.py`): every agent does real work via
`commit()` (`nodes.py`), then results are merged.

```json
{
  "round": 1,
  "strategy": "common_goal",
  "results": [ { "agent": "discharge_agent", "acted": true, "output": "…merged into the answer…" } ]
}
```

A **bidding round** → handler `rl_bidding` (`strategies.py`), in four beats:

1. ask each agent for an **offer** — a score, no action taken yet: `propose()` (`nodes.py`);
2. the highest offer wins;
3. the winner does the real work via `commit()` (`nodes.py`);
4. every loser stands down and does nothing via `skip()` (`nodes.py`) — recorded as skipped,
   not as an error.

```json
{
  "round": 2,
  "strategy": "bidding",
  "offers": [
    { "agent": "bed_agent_ward_A", "offer": 0.82 },
    { "agent": "bed_agent_ward_B", "offer": 0.41 }
  ],
  "winner": "bed_agent_ward_A",
  "results": [
    { "agent": "bed_agent_ward_A", "acted": true,  "output": "reserved the bed" },
    { "agent": "bed_agent_ward_B", "acted": false, "note": "stood down" }
  ]
}
```

```
  wrapped bidding round:
     agent A  offer 0.82  ┐
     agent B  offer 0.41  ┘ → highest wins → winner commits (acts), losers skip
```

This guarantees that when multiple agents contend for one unit, exactly one acts on it.

---

## How an agent computes its offer

Each agent needs its own logic to score its claim on the contested resource. These offer rules
are registered in `AGENT_BID_HOOKS` (`workflows/graph/agents/registry.py`).

Only the **bed agent** has one today. An agent with no rule returns a flat `0`, so a bidding
round made up only of such agents still runs the contest but ties at `0` — behaving like a
default round. Extending bidding to a new area means adding an offer rule for those agents here.

---

## Changing behavior

`workflows/strategies.json` is the single source of truth. Each entry's `description` and
`use when` text is the entire basis for selection — edit that text and you change when a
strategy is chosen, with no change to the prompt or the code.
