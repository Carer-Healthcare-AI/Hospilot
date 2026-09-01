# Bed generalization — from `icu_bed` to a bed family

**Goal.** One use case, *bed*, not necessarily ICU. A query phrased any way resolves to the
specific bed type it names; any hospital state drives it; the run ends in a real bid result.

**Status.** Plan only. Nothing below is implemented. Line numbers are hints and will drift —
symbol names are the durable reference.

---

## 0 · What already works

Do not rebuild these.

**The pipeline is complete.** `trigger/runtime.py` sequences all nine stages — use case,
ingest, utility, ceiling, budget, auction, settlement, audit, reward — and computes nothing
itself. Every number comes from a separately tested layer.

**Query resolution is already two-stage.**

1. **Token matcher** — `UseCaseMatcher.matched` in `allocation/profiles/registry.py`.
   Deterministic, offline, free. A query matches when it contains at least one *qualifier*
   (naming the unit) and one *noun* (naming the thing), in any order.
2. **LLM fallback** — `allocation/trigger/llm_matcher.py`. Consulted only when the token
   matcher finds nothing. Opt-in via `ALLOCATION_LLM_QUERY=1`, constrained by JSON schema to
   the registered resource types plus `"unknown"`, memoized per normalised query, and
   surfaced in the trace so a semantic resolution never masquerades as a token match.

The fallback already handles free-form phrasing — *"a spot is opening up in intensive care at
3pm"* → `icu_bed` is a worked example in its own prompt. **"Any phrasing" is ~90% built.** What
is missing is that it knows exactly one bed.

**The world is already injectable.** Hospital state, candidates, query and clock all enter
through the `DataSource` seam via three routes: request body (`POST /auction` with `hospital` +
`candidates`), a named scenario, or the Appendix C fixture. No code change is needed to drive
the engine from outside.

**Config is deliberately *not* injectable.** `caps.yaml` and the rule tables are pinned at
process start via `--config-dir`. This is correct and must not change — see the reasoning in
`api/service.py` (`Settings`), `config/loader.py` (`caps_version` is stamped on every utility
and budget row), and the `unsigned` governance mechanism. Caps are governance artifacts, not
inputs.

---

## 1 · Four findings that are easy to miss

These cost real time to re-derive. Read them before starting.

### F-A · `caps_config` and `budget_config` are dead fields

`ResourceProfile` declares both (`profiles/registry.py` ~L85-86):

```python
caps_config: str = "caps.yaml"
budget_config: str = "budget.yaml"
```

A repo-wide grep returns **only those two declaration lines — zero readers.** Meanwhile
`load_config` hardcodes `base / "caps.yaml"`. The per-resource caps seam is *declared but
never wired*. This is the intended design; it just was never connected.

### F-B · The LLM prompt hardcodes the HDU/PACU exclusion as prose

`llm_matcher._system_prompt()` builds its resource *list* from the registry, but this line is
literal text:

> *"In particular: HDU, PACU and high-dependency beds are alternative pathways, not ICU beds
> — they are `"unknown"`."*

Register `hdu_bed` and the model will **still refuse to resolve HDU queries.** The prompt must
be rewritten before any second bed type can be reached semantically.

### F-C · `HospitalState` is ICU-shaped, and it leaks at two boundaries

Not just the dataclass. Both of these must change together:

- `contracts.HospitalState` — `icu_total_beds`, `icu_occupied_beds`,
  `predicted_icu_demand_4h`, and `occupancy` divides by ICU totals.
- `ingest/scenarios._hospital` — requires those literal key names in any injected or
  scenario-supplied state.

So hospital state *can* be injected today, but only in ICU vocabulary. A ward-bed auction
would have to put ward beds in a field named `icu_total_beds`.

This also falsifies the claim in `profiles/registry.py` that adding a resource type touches
only `profiles/` plus config rows. That docstring invites the check — *"worth writing a
throwaway second profile during design purely to check"* — and the check fails here.

### F-D · `Operational` has no ICU branch, on purpose

`utility/components/operational.py` returns `Signal.absent` for `AgentKind.ICU`, because
*"inventing one would put a number on a department whose operational burden nobody has
defined."* Making ICU a bidder (needed for ward beds — ICU wants to step patients down to free
its own capacity) is therefore **not just an eligibility flag**; its operational formula has
to be defined first, or ICU permanently bids at reduced coverage.

---

## 2 · Open decisions — settle before Step 1

Each changes the code. None is derivable from the data.

| # | Decision | Notes |
|---|---|---|
| **D-1** | Which bed types? | `hdu_bed`, `ward_bed`, `pacu_slot`, some subset? Determines how many caps files and profiles exist. |
| **D-2** | What does a unit-less query do? | *"Who should get the next available bed?"* names no unit. **Recommend: raise, listing the bed types** — consistent with the existing no-default-profile rule, since scoring a ward bed against ICU caps yields a plausible number valid for nothing. |
| **D-3** | Budget: one pool or one per resource type? | Budgets are denominated in utility points. ICU-bed utilities (~107) dwarf ward-bed ones. A shared pool means ICU auctions drain it and departments rationally stop bidding on ward beds. Changes the config layout, so settle it early. |
| **D-4** | Scarcity scope | `budget.yaml` declares `scope: global`, justified as *"an identical multiplier on every budget alters no relative position."* That reasoning breaks across bed types: ICU at 100% and wards at 60% cannot share one scarcity value. |

---

## 3 · The steps

### Phase 1 — Make "bed" a family

**Step 1 · Add bed types to `ResourceType`**
`allocation/contracts.py`. Add the types chosen in D-1 alongside `ICU_BED`. This enum keys the
profile registry, `ReleaseEvent.auction_key`, and every audit row — it cascades, so it goes
first.

**Step 2 · One profile per bed type, sharing a `BedProfile` base**
New `profiles/hdu_bed.py`, `ward_bed.py`, … Each carries its own qualifiers, eligible agents,
TTLs, horizon, and caps file.

Write `hdu_bed` **first, deliberately as the throwaway seam check** described in F-C. It is
what proves Phase 2 is necessary rather than assumed.

**Step 3 · Rewrite the LLM system prompt** — see **F-B**
Delete the HDU/PACU exclusion. Replace with: resolve to the specific unit named; answer
`"unknown"` when no unit is identifiable. Keep the "prefer unknown over a guess" instruction —
that one is still right.

**Step 4 · Decide unit-less query behaviour** — see **D-2**
`trigger/query.resolve_profile` currently raises on >1 match. A bare "bed" query will now match
many or none.

**Step 5 · Wire `caps_config` through `load_config`** — see **F-A**
Split `caps.yaml` into `caps_icu_bed.yaml`, `caps_hdu_bed.yaml`, … and have the profile select
its own.

> ⚠ **`caps_version` must hash the file actually used.** Otherwise a ward-bed auction and an
> ICU-bed auction stamp identical versions and every audit row lies — which breaks B.13 cap
> fitting and any later re-derivation.

Each bed type needs its **own** eight maxima. The existing ones were chosen (never fitted) for
an ICU bed; reusing them inherits a calibration that was never valid — `profiles/registry.py`
states this as a hard rule.

### Phase 2 — Unit-scope the world

**Step 6 · `HospitalState` → unit-scoped** ← **start here**
`icu_total_beds` → `unit_total_beds`, `icu_occupied_beds` → `unit_occupied_beds`,
`predicted_icu_demand_4h` → `predicted_demand_4h`, plus a `unit:` field naming what is
described.

Six consumers:

| Consumer | File |
|---|---|
| `occupancy`, `isolation_pressure` | `allocation/contracts.py` |
| Resource Stress demand factor | `allocation/utility/components/resource_stress.py` |
| Budget factors (`compute_factors` call) | `allocation/trigger/runtime.py` |
| Contention (occupancy stress) | `allocation/auction/settle.py` |
| Reserve price | `allocation/auction/reserve.py` |
| Ingest trace row | `allocation/trigger/runtime.py` |

Accept **both** spellings in `ingest/scenarios._hospital` for one release so existing scenario
files and API callers do not break.

**Step 7 · Resolve Scarcity scope** — see **D-4**

**Step 8 · Alternative Availability → a relative ladder**
Today `AL = -20 × Quality × Duration` scores a fallback that is implicitly *below* ICU. Add an
ordered `care_ladder` to `rules/units.yaml` (`icu > hdu > pacu > resus > ed > ward`) and compute
against the best alternative *to the unit being auctioned*, excluding that unit itself.

Escalation and de-escalation must be distinguishable: auction an HDU bed and ICU sits *above*
it — scoring ICU as a comfortable fallback (quality 1.0 → −20, "you need this less") is the
wrong reading of a scarcer, better bed.

This is the **largest single discriminator in the utility** (F-16) and drives two of three
withdrawals in the worked example. Highest clinical consequence of any step here.

**Step 9 · "ICU benefit" → "target-unit benefit"**
`rules/icu_benefit.yaml` becomes per-resource, selected by the profile. Same 0.25 weight slot
inside Clinical Benefit, different table per bed type. For a ward bed the question inverts —
*is a ward bed sufficient* rather than *does ICU help*.

**Step 10 · Per-profile eligible agents** — see **F-D**
`AgentKind.ICU` is declared but eligible nowhere. For `ward_bed` it is the natural bidder.
Define its operational formula first.

### Phase 3 — Budget

**Step 11 · Implement the D-3 pooling decision.** Uses `budget_config` — the other dead field
from F-A.

### Phase 4 — Real data

Nothing is live today. There is **no database adapter** — only `FixtureDataSource` and
`scenarios.py`; no httpx / psycopg / asyncpg / GraphQL anywhere in `allocation/`.

**Step 12 · Build the `DataSource` adapter.** Two methods only (`contracts.DataSource`).
`hospital_state` now needs the unit so it reads the right beds.

**Step 13 · Staleness guards.** Stale input must produce `Signal.absent`, never a stale value.
The three-state rule ("absent is not zero") only holds if the adapter honours it. The auction
is 3 rounds × 120s against a bed ~30 min out, with 10–20 min TTLs — a 3-hour-old vitals reading
makes the deterioration slope meaningless.

**Step 14 · Per-unit release triggers + reconciliation** into `allocation.auction_outcome`. A
predicted discharge that does not happen means a `LIVE` auction held a bed that never freed.

**Step 15 · Only then unblock LIVE.** Both `api/service.check_mode` and the CLI refuse `live`
outright today, because the shipped source serves three invented patients.

---

## 4 · Live-data inventory

What the adapter in Step 12 has to supply, by readiness.

**Tier A — real source exists, needs only the adapter**

| Data | Source | Feeds |
|---|---|---|
| Vitals | `hospilot.vitals` | NEWS2, deterioration slope, oxygen severity/trend, vital trend |
| Labs | `hospilot.lab_results` | Organ risk (lactate, creatinine, bilirubin, platelets) |
| Bed state | `hospilot.beds` | Occupancy, unit classification |
| Forecasts | `/icu/demand`, `/discharge/volume` | Demand pressure, downstream impact |
| ER pressure | `/er/boarding`, `/er/lwbs` | Queue impact, ER operational |
| Isolation | `infection_cases` | Isolation pressure |
| Nursing | `nursing_tasks` + `staff_roster` | Nursing capacity, Ward operational |
| Revenue | `contract_service_rates`, `claims` | Expected revenue |
| Elapsed time | `arrived_at` | DelayFactor — **needs no model at all** |

Mind **F-08** naming drift: `bp_sys` → `bp_systolic`, `result_time` → `reported_at`,
`patient_id` → `patient_token`. `nursing_tasks` has no `status` (use `completed = false`) and no
ward (join via `admission_id → ipd_admissions → beds.ward`).

**Tier B — migration written, unsigned**

- `on_oxygen` (B.1, migration 092). Without it NEWS2 silently drops 1 of 7 parameters on
  **every** patient.
- Forecast retention (093) → the 30-day median behind the Demand factor. Until it lands,
  Demand falls back to 1.0 — it is *not a measurement*.

**Tier C — no source at all; needs a human, not an adapter**

- **Capability vectors + safe-hold hours** (B.7 / B.4) — largest discriminator in the utility,
  entirely invented, neither table in the schema. One clinical governance workshop.
- Target-unit benefit rule table (B.10) — needs records of patients *denied* a bed, which
  nothing stores.
- Cost table (B.6) — Finance.
- `P(no PACU capacity)` (F-05) — `ot_room_status` has no recovery-area concept.
- Age / comorbidity — no DOB in `hospilot.patients`; permanently absent → 90% coverage on
  every patient.

**Tier D — buildable today, nobody blocked**

- B.5 time-to-critical, B.8 `P_det`. Both trainable from vitals history already held.

**Fix first:** **F-30** — `pharmacy_orders` is fetched, logged, and scored by **nothing**.
Removing the noradrenaline order from the septic-shock patient changes utility by 0.0. The data
is captured; the consumer is missing.

---

## 5 · Order of work

```
D-1 … D-4  decide
   │
Step 6     unit-scope HospitalState        ← blocks 7, 8, 12 and every second profile
   │
Steps 1,2,5  types · hdu_bed probe · caps wiring
   │
Steps 3,4  query resolution                  (independent — can run in parallel)
   │
Steps 8,9,10  clinical correctness
   │
Step 11    budget pooling
   │
Steps 12-15  live data, then LIVE
```

**Start at Step 6.** It is self-contained, touches six call sites, and keeping both field
spellings accepted at the parse boundary means nothing existing breaks.
