# Bed generalization — working checklist

Derived from [BED_GENERALIZATION_PLAN.md](BED_GENERALIZATION_PLAN.md). Ordered by §5 *Order of
work*, not by step number. Tick as we finish; record decisions inline so they survive the session.

Legend: `[ ]` open · `[x]` done · `[~]` in progress · `[-]` dropped (say why)

---

## Status — 2026-08-12

**Stages 0–5 complete; Stage 6 half done. 430 tests green.** All six bed types auction end to
end, each against its own caps table, its own budget pool, and — as of this session — **its own
unit's beds**.

| Stage | Steps | State |
|---|---|---|
| 0 · Decisions | D-1 … D-4 | ✅ settled |
| 1 · Unit-scope `HospitalState` | 6 | ✅ done |
| 2 · Types · `hdu_bed` probe · caps | 1, 2, 5 | ✅ done (caps unfitted — see C-1) |
| 3 · Query resolution | 3, 4 | ✅ done |
| 4 · Clinical correctness | 8, 9, 10 | ✅ done (benefit tables empty — see C-3) |
| 5 · Scarcity & budget | 7, 11 | ✅ done (pools unsized — see C-2) |
| 6 · Real data, then LIVE | 12 (seam) | ✅ done — the behavioural gap is closed |
| | 12 (Tier A), 13–15 | ⬜ **needs a database.** Not buildable here — see below |

**The behavioural gap is closed.** `DataSource.hospital_state(unit, at)` now takes the unit of
the bed being auctioned, so an HDU auction reads HDU's beds. Every occupancy consumer —
Resource Stress, the reserve price, contention, scarcity, every budget factor — follows from
that one argument. The six fixture units have six deliberately distinguishable occupancies, so
reading the wrong one is a wrong number rather than a plausible one.

**What Step 12 still owes, and why it stopped here.** The step is two things: the seam (done)
and the Tier A database wiring (not started). There is no database in this repository — no
`hospilot` schema, no psycopg / httpx / asyncpg / GraphQL dependency, no credentials, and
`db/migrations/` holds only `allocation`'s own three files. A Hasura reader written against a
schema nobody here can query, run or test would be precisely the "plausible number valid for
nothing" this file keeps refusing. Steps 13–15 all sit behind it.

**Where to start a fresh session:** read *Deferred caveats* below. C-1…C-4 and the new **C-9**
need a human decision and block nothing in code. Steps 12(Tier A)–15 need database access.
If neither is available, the useful work left is under *Cross-cutting* — F-30 and Tier D.

---

## Deferred caveats — take these in order

Everything knowingly left undone, with why. **C-1 … C-4 and C-9 are one governance workshop**,
not code — they are all the same failure: a number nobody fitted, marked honest rather than
made correct. Each is reported by `Config.unsigned`, by coverage, or by a test that pins the
defect on every run.

C-9 is the newest and the only one this session *created*: closing the behavioural gap made a
constant that had never been exercised start to matter. That is the normal shape of this work —
until each auction read its own unit, an ICU-shaped threshold could not be observed to be
ICU-shaped.

| # | Caveat | Why deferred | Surfaces as |
|---|---|---|---|
| **C-1** | **Five caps tables are verbatim copies of ICU's.** `caps_{hdu,pacu,resus,ed,ward}_bed.yaml` | The eight maxima were chosen (never fitted) for an ICU bed; there is no data to fit the others. Copying is honest only because it announces itself. | `unsigned: caps.ward_bed = unfitted_copied_from_icu_bed` |
| **C-2** | **Five budget pools are copies too, all carrying ICU's Base of 700.** | Pool *separation* fixes D-3's failure and is done. Pool *sizing* needs observed burn rates per resource type. Ward-bed utilities are smaller, so the same Base makes the constraint **looser** there — below a 0.40 burn rate AGENT_BUDGET §8 says the budget is inert and *"bidding maximum is free"*. | `unsigned: budget.pool.ward_bed = unfitted_copied_from_icu_bed` |
| **C-3** | **Five target-unit benefit tables are empty.** | The question *inverts* across the family — for a ward bed it asks whether a ward bed is *sufficient*, so ICU's numbers are wrong rather than approximate. Each section carries the `question` the workshop must answer; that write-up is the deliverable. | Clinical Benefit coverage drops 90% → 65% |
| **C-4** | **ICU's Operational formula is nursing saturation with the *ward's* constant.** | Chosen, not measured (your call). It measures nursing load, not the bed pressure ICU actually bids on; 8 tasks/nurse was set for a ward and ICU nurses at 1:1 don't saturate at the same ratio. | Signal note on every ICU score |
| **C-5** | **`budget.yaml` has no `targets` row for `icu`.** | Harmless under the shipped `base.mode: common`; under `derived` ICU would get a base of 0. Now raises with a clear message rather than failing silently. | `KeyError` naming the cause |
| ~~**C-6**~~ | ~~`resource_stress._demand` reads its field via `getattr(..., None)`~~ | **CLOSED this session.** Both readers take a typed `HospitalState` and access their fields directly, so a rename raises instead of degrading every patient to `Signal.absent`. Pinned by `test_a_missing_forecast_is_absent_and_a_renamed_field_is_not` — which asserts *both* halves, because the genuine-absence path had to survive the tightening. | — |
| **C-7** | **The LLM fallback has never run against a real model.** | `anthropic` is not installed and no API key is set. Tests pin the contract around the call — gating, memoization, tie-break rules, trace honesty, request shape — but **not** Claude's judgement of the rewritten prompt. | Nothing until someone sets `ALLOCATION_LLM_QUERY=1` |
| **C-8** | **Legacy `icu_*` hospital-state keys still parse.** | Deliberate: one release of compatibility (F-C). Remove after downstream callers migrate. | `_LEGACY_HOSPITAL_KEYS` in `ingest/scenarios.py` |
| **C-9** | **The 0.85 occupancy onset is a fraction fitted for a 20-bed ICU, and three mechanisms start there.** Scarcity (`onset_occupancy`), contention (`occupancy_onset`) and the reserve price all begin at 0.85 with a 0.15 span. | **New, and newly *reachable*: Step 12 caused it.** While every auction read ICU's 20/20 all three sat at their ceiling in every auction; now each unit's real occupancy is used and small units fall off the bottom. A fraction is not scale-free — 0.85 of resus's 6 beds is 5.1, so the whole 0.85–1.00 band falls between its 5th bed and its 6th, and a resus down to **one free bed scores as unstressed**. PACU (8 beds) gets one intermediate point; a 40-bed ward has a usable gradient, so the constant is defensible there and nowhere small. Worse, below the onset the reserve moves the *wrong way*: filling the unit lowers the minimum acceptable bid, because the only occupancy term still live is Resource Stress, which lowers the ceiling the reserve is a fraction of. Fitting an onset per unit is the same governance workshop as C-1…C-4. | Pinned, not silent: `test_the_occupancy_onset_has_no_gradient_left_for_a_small_unit` asserts the flat band and the inverted reserve, so nobody adjusts 0.85 believing it is already per-unit |

---

## Stage 0 · Decisions (block everything) — **settled 2026-08-12**

- [x] **D-1 · Which bed types?** → **The whole family. Cover every kind of bed, because the
      available bed is not known in advance and a query may not name a department at all.**
      Grounded in `config/rules/units.yaml`, which already declares the taxonomy and already
      has the catch-all this decision needs:
      `icu · hdu · pacu · resus · ed · ward`, matched case-insensitively in order from the
      free-text `hospilot.beds.ward`, with `{pattern: "", unit: ward}` as the default.
      Same list as the Step 8 `care_ladder`, so auctionable set == ladder set.
      → *open sub-question, does not block Step 6:* are `ed` and `resus` **auctionable**
      resources, or alternatives-only rungs on the ladder? See Stage 2.
- [x] **D-2 · Unit-less query behaviour.** → **Defer to the LLM fallback.** Let
      `llm_matcher` infer the unit from context; `"unknown"` only when it genuinely cannot.
      Supersedes the plan's "raise, listing the bed types" recommendation.
      → *residual, does not block Step 6:* what happens after the LLM **also** returns
      unknown. Today that raises `UnknownUseCase`. Settle during Step 4.
- [x] **D-3 · Budget pooling.** → **One pool per resource type.** ICU-bed utilities (~107)
      dwarf ward-bed ones; a shared pool means ICU auctions drain it and departments
      rationally stop bidding on ward beds. Costs a config-layout change + per-type budget
      files. Implemented in Step 11 via the dead `budget_config` field (F-A).
- [x] **D-4 · Scarcity scope.** → **Per-resource-type.** The existing `scope: global`
      justification — *"an identical multiplier on every budget alters no relative
      position"* — breaks across bed types: ICU at 100% and wards at 60% cannot share one
      scarcity value. Implemented in Step 7.

---

## Stage 1 · Step 6 — unit-scope `HospitalState` — **done 2026-08-12**

Blocked Steps 7, 8, 12 and every second profile. **357 tests green.**

- [x] Rename fields in `contracts.HospitalState`
      ([contracts.py:293](allocation/contracts.py#L293)):
      `icu_total_beds` → `unit_total_beds`, `icu_occupied_beds` → `unit_occupied_beds`,
      `predicted_icu_demand_4h` → `predicted_demand_4h`
- [x] Add `unit:` field naming what the state describes. Plain `str`, matching
      `Candidate.current_unit` — the taxonomy is a signed-off config table in
      `rules/units.yaml`, not a code constant, so it is deliberately not an enum
- [x] Accept **both** spellings in `ingest/scenarios._hospital` for one release (F-C).
      Legacy keys imply `unit: icu` — there was no other unit they could have meant
- [x] **No default unit.** New spelling with no `unit:` raises rather than guessing; the
      error names the *current* spelling so it teaches the new vocabulary

Consumers — the plan listed six; **only four needed touching**:

- [x] `occupancy`, `isolation_pressure` — [allocation/contracts.py:301](allocation/contracts.py#L301)
- [x] Resource Stress demand factor — [resource_stress.py:55](allocation/utility/components/resource_stress.py#L55)
- [x] Budget factors (`compute_factors` call) — [runtime.py:309](allocation/trigger/runtime.py#L309)
- [x] Ingest trace row — [runtime.py:237](allocation/trigger/runtime.py#L237); the hardcoded
      `"ICU occupancy"` label is now `f"{hospital.unit.upper()} occupancy"`
- [x] ~~Contention (occupancy stress)~~ — [settle.py](allocation/auction/settle.py) and
      [spend.py](allocation/budget/spend.py) take `occupancy: float` as a parameter. The
      property already insulates them; **no change needed**
- [x] ~~Reserve price~~ — [reserve.py:27](allocation/auction/reserve.py#L27) likewise takes
      `occupancy: float`. **No change needed**

Also carried:

- [x] `ingest/fixtures.HOSPITAL_STATE` → new spelling, `unit="icu"`
- [x] Audit rows get `unit` for free — `serialise.hospital_state` walks dataclass fields
- [x] Migrated our own inputs: [scenarios/ward_crash.yaml](scenarios/ward_crash.yaml),
      [examples/patients.json](examples/patients.json), [README.md](README.md),
      `api/app.py` request-body description
- [x] Three new tests pin the contract, since our own fixtures no longer exercise the legacy
      path: legacy keys → `unit: icu` · a **ward** state parses and reports 60% occupancy ·
      new spelling without `unit:` raises

> **C-6 · Note for Step 12.** `predicted_demand_4h` is read via `getattr(state, ..., None)` in
> `resource_stress._demand`, so a future field rename fails silently to `Signal.absent`
> rather than erroring. Worth tightening when the adapter lands.

---

## Stage 2 · Steps 1, 2, 5 — types · `hdu_bed` probe · caps wiring — **done 2026-08-12**

**367 tests green.** All six bed types auction end to end.

**Step 1 · `ResourceType`** — [allocation/contracts.py:48](allocation/contracts.py#L48)

- [x] D-1 sub-question settled per your answer: **all six are auctionable.** `ed` and `resus`
      are single-bidder auctions (ER only) — they run and clear, but with one bid there is
      nothing to discriminate between, so contention and the reserve margin read differently.
      Recorded in both profiles' notes.
- [x] `HDU_BED`, `PACU_BED`, `RESUS_BED`, `ED_BED`, `WARD_BED` alongside `ICU_BED`
- [x] Spelling settled: **`pacu_bed`**, not the plan's `pacu_slot`, so every member reads
      `<unit>_bed`
- [x] Added `ResourceType.unit` (`icu_bed` → `icu`) — ladder lookup by string, no mapping table
- [x] Cascade verified: registry keys, `auction_key`, audit rows

**Step 2 · Profiles on a shared base** — [allocation/profiles/bed.py](allocation/profiles/bed.py)

- [x] `bed_profile()` factory rather than a subclass — `ResourceProfile` is a frozen
      `slots=True` dataclass, so one type keeps flowing through the engine
- [x] [hdu_bed.py](allocation/profiles/hdu_bed.py) written **first as the F-C probe**
- [x] [ward_bed.py](allocation/profiles/ward_bed.py) · [pacu_bed.py](allocation/profiles/pacu_bed.py)
      · [resus_bed.py](allocation/profiles/resus_bed.py) · [ed_bed.py](allocation/profiles/ed_bed.py)
- [x] `icu_bed` moved onto the same base
- [x] **`AgentKind.ICU` stayed ineligible everywhere at this stage** — gated on the F-D
      operational formula. *Superseded in Stage 4:* Step 10 defined the formula, and ICU is now
      eligible for `ward_bed` and `hdu_bed`.
- [x] Corrected the `registry.py` docstring — it now says the probe was run, that it **failed
      twice**, and to re-run it rather than trust the paragraph

**Step 5 · `caps_config` wired** (F-A: declared, zero readers)

- [x] `caps.yaml` split into six `caps_<resource>.yaml`; the old file is **deleted**
- [x] `Config.for_resource(profile)` selects the table; `run_allocation` calls it once, right
      after profile resolution, so everything downstream inherits it
- [x] ⚠ **`caps_version` hashes the file actually used** — verified live: an HDU run stamps
      `dc667a35cdc9`, an ICU run `35d2edcecb59`. New test
      `test_editing_another_beds_caps_leaves_this_run_alone` pins it.
- [x] `caps_config` **default removed** — a profile that forgets its caps file now fails
      instead of quietly inheriting ICU's
- [x] `Config.unsigned` now reports caps status, so an unfitted table announces itself on
      every run: `{'caps.hdu_bed': 'unfitted_copied_from_icu_bed'}`
- [ ] **C-1 · Fit each bed type's own eight maxima.** *Not done and not doable here* — five of six
      caps files are verbatim copies of the ICU table. They are marked
      `unfitted_copied_from_icu_bed` and surfaced by `Config.unsigned`, which makes them
      honest, not correct. Needs the same governance workshop as Tier C.

### Step 4 arrived early — the founding query forced it

Registering `ward_bed` broke *"ER, OT, and ICU/Ward demand compete for one limited ICU bed"*:
it names **two** units, because it lists bidders alongside the resource. Refusing it would
refuse the one query the engine exists to answer.

- [x] Added `UseCaseMatcher.distance()` — fewest tokens between any qualifier and any noun
- [x] `resolve_profile` uses it **only to break a tie** between profiles that both matched,
      never to accept a query no profile matched. A genuine two-resource query
      (*"an ICU bed and a ward bed"*) still ties, and a tie still raises.
- [x] Same hazard avoided in qualifiers: `ed_bed` deliberately excludes `"er"` and bare
      `"emergency"` (`er` is an `AgentKind`), `pacu_bed` excludes bare `"recovery"`

> **Reality check on the output.** *As of Stage 2*, ICU, HDU and ward auctions produced
> *identical* utilities (107.4 / 35.1 / 48.6) — the family was wired but not discriminating.
> Three causes: copied caps (Step 5), Alternative Availability not knowing the target unit
> (Step 8), and one benefit table for all beds (Step 9).
>
> **Stage 4 fixed two of the three, Stage 6 the third.** Ward-Patient-C scores 48.6 in an ICU
> auction and 62.6 in an HDU one, Clinical Benefit coverage drops from 90% to 65% on beds whose
> benefit question is undefined, and **each auction now reads its own unit's occupancy**
> (Step 12a). What remains of the original list: the copied caps (C-1).

---

## Stage 3 · Steps 3, 4 — query resolution — **done 2026-08-12**

**374 tests green.**

**Step 3 · LLM system prompt rewritten** — [allocation/trigger/llm_matcher.py:84](allocation/trigger/llm_matcher.py#L84)

- [x] Deleted the hardcoded HDU/PACU exclusion (F-B). It was literal prose while the resource
      *list* was generated, so after Stage 2 the prompt named `hdu_bed` and forbade HDU in the
      same breath.
- [x] Replaced with three generated-list-proof rules: **resolve to the unit the query names**
      · **name the unit, not the bidder** · **`"unknown"` when no unit is identifiable**
- [x] Kept "prefer unknown over a guess", with the reason (a wrong resolution is undetectable
      downstream; an unknown just asks the operator to name the unit)
- [x] Worked examples now span three bed types, and the bidder-vs-resource examples encode
      what Stage 2 learned the hard way
- [x] Two guard tests: the prompt lists **every** registered resource, and it contains no rule
      forbidding a unit it offers — shape-based, so the next unit added can't reintroduce F-B

**Step 4 · Query resolution** — `trigger/query.resolve_profile`

- [x] >1 token match: nearest qualifier-to-noun wins (added in Stage 2)
- [x] D-2 implemented — the fallback now runs whenever the tokens are **inconclusive**, which
      is two cases, not one: nothing matched, *or* several matched equally. The second is the
      ambiguity a bed family creates and it did not exist when D-2 was framed.
- [x] **On a tie the model may only choose among the profiles the tokens already found.**
      Tokens establish which readings are plausible; the model disambiguates between them.
      An answer outside that set is an override, not a disambiguation, and raises.
- [x] D-2 residual settled: LLM `"unknown"` still raises `UnknownUseCase`, as do disabled ·
      SDK missing · timeout · refusal. No default profile survives intact.
- [x] `matched_tokens` checks the memo **before** the tokens — a tie broken by the model
      reports `ward + bed (ambiguous with [...]; llm chose this one)` rather than hiding it
- [x] Tests cover the previously-unexercised path: tie with fallback off · tie broken · answer
      outside the token set rejected · trace disclosure · and that the founding query is
      settled by proximity **without ever calling the model**

**Addendum · the request itself** — [llm_matcher.py:148](allocation/trigger/llm_matcher.py#L148)

- [x] `thinking={"type": "disabled"}` set explicitly. Thinking is **on by default** on
      `claude-opus-5` (a change from Opus 4.8), and `max_tokens` caps thinking and response
      text *together* — so a 1024 budget meant for `{"resource_type": ...}` was shared with
      reasoning a fixed-schema classification does not need. A long think returns
      `stop_reason: "max_tokens"`, the guard returns `None`, and the query degrades to
      `UnknownUseCase` indistinguishably from a genuine unknown. Legal here because effort is
      `low`; the API rejects disabled thinking only at `xhigh`/`max`.
- [x] Four tests pin the request shape via a stub SDK module — the only way to reach
      `_ask_claude`, which otherwise returns at the `ImportError` before touching a parameter.
      They catch: thinking left on against a small budget · effort raised to `xhigh` while
      thinking is disabled (a 400) · `fallbacks: "default"` crossed with the array-form beta
      header (also a 400) · the schema drifting from the registered resource set.
- [ ] **C-7** — none of this exercises the model's *judgement*. See the caveat register.

---

## Stage 4 · Steps 8, 9, 10 — clinical correctness — **done 2026-08-12**

**396 tests green**, including a new [tests/test_bed_family.py](tests/test_bed_family.py).

**Step 8 · Alternative Availability → a relative ladder** — *highest clinical consequence here*

- [x] `care_ladder: [icu, hdu, pacu, resus, ed, ward]` added to
      [rules/units.yaml](allocation/config/rules/units.yaml)
- [x] Scored against the best alternative **to the unit being auctioned**; the target unit is
      excluded from its own alternatives
- [x] Escalation vs de-escalation separated by `escalation_penalty_multiplier: 0.0` — an
      alternative *above* the target contributes no penalty, so the score is the best
      remaining de-escalation, or zero if there is none
- [x] `PatientData.alternative_units` added so the component has candidates to choose between;
      `best_alternative_unit` still read as the single-value shorthand
- [x] Zero here stays a **finding, not an absence** — the note names which units were
      considered and why none qualified, so it cannot be mistaken for missing data
- [x] Worked example unchanged: every Appendix C alternative is below ICU, so the ICU auction
      still scores −13.0 / −14.0. Pinned by a regression test.
- [x] Verified it bites where it should: Ward-Patient-C's HDU fallback drops in an **HDU**
      auction (48.6 → 62.6); in a **ward** auction every alternative is an escalation

**Step 9 · "ICU benefit" → "target-unit benefit"**

- [x] `rules/icu_benefit.yaml` → [rules/unit_benefit.yaml](allocation/config/rules/unit_benefit.yaml),
      one section per resource type, selected by the profile
- [x] Same 0.25 weight slot; factor renamed `icu_benefit` → `unit_benefit` across all six caps
      files and the audit / unsigned keys
- [x] **The five non-ICU tables are empty on purpose.** The question inverts — for a ward bed
      it asks whether a ward bed is *sufficient*, and a high ICU-benefit score implies a *low*
      score there — so ICU's numbers are not a starting point, they are wrong. Empty → absent
      → dropped and renormalised (D.0), costing visible coverage: ICU 90%, ward 65%.
- [x] Each empty section carries the `question` the workshop must answer — that write-up is
      the actual deliverable, and a test asserts every resource has one

**Step 10 · Per-profile eligible agents** (F-D)

- [x] **`AgentKind.ICU`'s operational formula defined** — nursing saturation, the same shape as
      Ward's (your call). [operational.py](allocation/utility/components/operational.py)
- [x] ICU eligible for `ward_bed` **and** `hdu_bed` — both step-down destinations
- [x] ICU stays ineligible for `icu_bed`, `pacu_bed`, `resus_bed`, `ed_bed`. Whether ICU bids
      for an *ICU* bed as internal demand is a separate open question (F-12 / AGENT_BUDGET
      decision 3) that Step 10 does not settle.
- [x] Verified ICU scores at **100% Operational coverage**, not the handicap F-D warned about
- [x] Appendix C still has exactly three bidders — pinned by a test

> **C-4 · Two caveats recorded on the ICU formula itself**, both in the signal note on every score
> rather than buried here:
> 1. It measures **nursing load, not bed pressure**. ICU wants the step-down bed because its
>    own beds are full, and `HospitalState` is now unit-scoped to the bed being *auctioned* —
>    reading ICU's own occupancy needs the **Step 12** adapter.
> 2. The saturation constant (8 tasks/nurse) was **chosen for a ward**. ICU nurses at 1:1 do
>    not saturate at the same ratio, and nobody has fitted an ICU figure.
>
> Also: `budget.yaml` has no `targets` row for `icu`. Harmless under `base.mode: common` (the
> shipped mode), but under `derived` ICU would get a base of 0.

---

## Stage 5 · Steps 7 + 11 — scarcity & budget — **done 2026-08-12**

**403 tests green.**

**Step 7 · Scarcity scope → per-resource-type** (D-4)

- [x] `scope: global` → `scope: per_resource_type` in every budget file
- [x] Justification rewritten. The original — *"an identical multiplier on every budget alters
      no relative position"* — is still exactly right **across agents** and was never right
      **across bed types**: scarcity is computed from the occupancy of the unit being
      auctioned, so ICU at 100% and wards at 60% are two numbers about two different units.
      Collapsing them would apply the wrong unit's occupancy to the right unit's auction.
- [x] Per-resource budget files make the scope per-resource by construction

**Step 11 · Budget pool per resource type** (D-3)

- [x] `budget.yaml` split into six `budget_<resource>.yaml`; the old file is **deleted**
- [x] `budget_config` wired — the other dead F-A field. Default removed, like `caps_config`.
- [x] `Config.for_resource` now selects **both** caps and budget
- [x] **The failure mode is unreachable, not merely discouraged.** A `BudgetState` stamps the
      `caps_version` its points were denominated in, and caps are per resource — so
      `run_allocation` refuses budgets carried in from another resource type. Tested both
      ways: cross-resource refused, same-resource sequences still work.
- [x] `Config.unsigned` reports the selected pool only:
      `budget.pool.ward_bed: unfitted_copied_from_icu_bed`

> **C-2 · Pool separation is real; pool *sizing* is not.** All six files carry ICU's Base of 700,
> which was set against ICU-bed utilities (~107). Ward-bed utilities are smaller, so the same
> Base makes the constraint proportionally **looser** there — and below a 0.40 burn rate
> AGENT_BUDGET §8 says the budget is inert and *"bidding maximum is free, and the RL will
> learn to do exactly that"*. Separate pools fix D-3's failure; sizing them needs observed
> burn rates per resource type. Same workshop as the caps.

**Two defects found and fixed while doing this**

- [x] `for_resource` silently discarded an in-place `replace(config, budget=...)` override, so
      a caller adjusting a table in memory would have been testing the shipped file instead.
      It now leaves an already-scoped table alone. Caught by an existing derived-mode test.
- [x] Under `base.mode: derived`, an agent with no `expected_utility` row got a base of **0**
      silently. Step 10 made this reachable by making ICU an eligible bidder — `icu` has no
      row. Now raises and names the cause.

---

## Stage 6 · Steps 12–15 — real data, then LIVE

Still nothing live: no database adapter, no httpx / psycopg / asyncpg / GraphQL anywhere in
`allocation/`. **The seam is now unit-aware, which is the half that did not need a database.**

### Step 12a · The seam — **done 2026-08-12**

**430 tests green.** This was the last behavioural gap in the engine.

- [x] `DataSource.hospital_state(unit, at)` — [contracts.py:581](allocation/contracts.py#L581).
      The docstring states the implementation obligation: an implementation that cannot
      describe the unit **raises**, never substitutes
- [x] `build_snapshot(..., unit)` — [snapshot.py:28](allocation/ingest/snapshot.py#L28) — and it
      **checks the answer**: a source returning another unit's state raises here. Nothing
      downstream reads `HospitalState.unit`, so an adapter that ignored the argument would
      otherwise be undetectable. The future Hasura reader crosses this same seam
- [x] The unit comes from `profile.resource_type.unit`, **not from the bidders** —
      [runtime.py:229](allocation/trigger/runtime.py#L229). ER, OT and ICU can all bid for a
      ward bed; the occupancy that prices the auction is the ward's
- [x] `FixtureDataSource` serves a family: `UNIT_STATES`, six units, six **deliberately
      distinguishable** occupancies (1.000 · 0.833 · 0.818 · 0.750 · 0.600 · 0.500) so a
      mis-wiring is a wrong number rather than a plausible one. Passing a single
      `HospitalState` still works and describes exactly one unit — the state carries its own
      `unit`, so that is exact rather than defaulted
- [x] ⚠ **The five non-ICU states are invented, and say so.** Appendix C publishes one
      hospital state and it is the ICU's. The block carries its own caveat: a fixture is
      invented by definition and none of it reaches a real allocation, but
      `fixtures.py`'s "transcribed, not invented" now applies to `HOSPITAL_STATE` and the
      patients, not to that block
- [x] Refusing an undescribed unit raises **`ValueError`, not `KeyError`** — it is a mismatch
      between a question and a world, not a failed lookup, and the API already turns
      `ValueError` into a 400 carrying the message. A `KeyError` escaped as a 500 with the
      text wrapped in quotes
- [x] Scenarios and request bodies can describe several units — `hospital.units:`, with
      `boarding_count` / `lwbs_risk` shared because they describe the **ED's queue** rather
      than the beds under auction. A per-unit key left beside `units:` is refused **by name**;
      sharing a demand forecast across units is the same bug in miniature
- [x] [scenarios/step_down.yaml](scenarios/step_down.yaml) — the same three patients auctioned
      an ICU bed and a ward bed out of one file. Its header states the full five-component
      decomposition with a cause for each, and a test pins every number in it, because a
      header that drifts from the run is worse than no header
- [x] C-6 closed — see the caveat register
- [x] Two stale claims corrected: the budget trace said *"Scarcity is global"* (D-4 changed
      that in Stage 5) and a test docstring repeated it. The *conclusion* both drew — that
      Criticality is the only factor differentiating departments — is still exact

> **C-9 opened by this step.** The occupancy onset of 0.85 is a fraction fitted for a 20-bed
> ICU, and scarcity, contention and the reserve all begin there. It could not be observed to be
> ICU-shaped while every auction read ICU's 20/20. See the caveat register.

### Step 12b · Tier A database wiring — **blocked: no database**

Not started, and not startable here. No `hospilot` schema, no driver dependency, no
credentials; `db/migrations/` holds only `allocation`'s own three files. The seam above is what
this plugs into — `hospital_state(unit, at)` and `patient_data(candidate, at)`, two methods.

- [ ] Tier A wiring: vitals · labs · bed state · forecasts · ER pressure · isolation · nursing ·
      revenue · elapsed time
- [ ] Handle **F-08** naming drift: `bp_sys`→`bp_systolic`, `result_time`→`reported_at`,
      `patient_id`→`patient_token`; `nursing_tasks` has no `status` (use `completed = false`)
      and no ward (join `admission_id → ipd_admissions → beds.ward`)
- [ ] `hospital_state` maps its `unit` argument onto `hospilot.beds.ward` through
      `units.yaml`'s `ward_patterns` — the same table `Candidate.current_unit` is classified
      by. The catch-all `{pattern: "", unit: ward}` classifies an unrecognised *ward string*;
      it must not be allowed to answer for a unit the query actually named

**Step 13 · Staleness guards** — behind 12b

- [ ] Stale input produces `Signal.absent`, never a stale value — "absent is not zero" only
      holds if the adapter honours it (3 rounds × 120s, 10–20 min TTLs, bed ~30 min out)

**Step 14 · Per-unit release triggers + reconciliation** into `allocation.auction_outcome`

- [ ] Reconcile predicted discharges that don't happen — a `LIVE` auction held a bed that never
      freed

**Step 15 · Unblock LIVE — only then**

- [ ] `api/service.check_mode` and the CLI both refuse `live` today (shipped source serves three
      invented patients)

---

## Cross-cutting / unblocked-by-nothing

- [ ] **F-30 fix first** — `pharmacy_orders` is fetched, logged, and scored by **nothing**.
      Removing the noradrenaline order from the septic-shock patient changes utility by 0.0.
      Data captured, consumer missing.
- [ ] Tier D, buildable today: B.5 time-to-critical, B.8 `P_det` — both trainable from vitals
      history already held

**Tier B — blocked on unsigned migrations** (track, don't build)

- [ ] `on_oxygen` (B.1, migration 092) — without it NEWS2 silently drops 1 of 7 parameters on
      **every** patient
- [ ] Forecast retention (093) → 30-day median behind the Demand factor; until it lands Demand
      falls back to 1.0 and is *not a measurement*

**Tier C — needs a human, not an adapter** (no code until these land)

- [ ] Capability vectors + safe-hold hours (B.7 / B.4) — largest utility discriminator, entirely
      invented, neither table in schema. One clinical governance workshop — **the same workshop
      as C-1 … C-4 and C-9**; the `care_ladder` and `escalation_penalty_multiplier` added in
      Step 8 are unsigned under this file's existing `status` and belong on the same agenda.
      C-9 is the item on that agenda that Finance/Ops can answer alone: *at what occupancy does
      each unit become scarce?* — a per-unit number, not a fraction copied from the ICU.
- [ ] Target-unit benefit rule table (B.10) — needs records of patients *denied* a bed. **C-3** is the per-resource half of this: the tables exist and carry their questions, but only `icu_bed` has values
- [ ] Cost table (B.6) — Finance
- [ ] `P(no PACU capacity)` (F-05) — `ot_room_status` has no recovery-area concept
- [ ] Age / comorbidity — no DOB in `hospilot.patients`; permanently absent → 90% coverage

---

## Invariants — do not break

- Config stays **non-injectable**. Caps tables, budget pools and rule tables are all pinned at
  process start via `--config-dir`; `Config.for_resource` selects between tables already loaded
  and never re-reads the filesystem. Caps are governance artifacts, not inputs. See
  `api/service.py` (`Settings`), `config/loader.py`, and the `unsigned` mechanism.
- The pipeline in `trigger/runtime.py` computes nothing itself — every number comes from a
  separately tested layer. Keep it that way.
- No default profile. Scoring a ward bed against ICU caps yields a plausible number valid for
  nothing.
- **One caps table and one budget pool per resource type.** Both `caps_config` and
  `budget_config` are required fields with no default — a profile that forgets either fails
  with a missing-file error rather than silently inheriting ICU's. `caps_version` hashes the
  caps file *actually used*, so audit rows stay re-derivable.
- **A budget from one resource type is never spendable on another.** `run_allocation` refuses
  carried budgets whose `caps_version` does not match the run's; utility points from two caps
  tables are not the same unit.
- **Absent is not zero.** An unfitted table left empty (C-3) must degrade to `Signal.absent`
  and cost visible coverage — never to a plausible number nothing downstream can detect.
- **A data source answers for the unit it was asked about, or it raises.** Never another unit's
  beds. `hospital_state` takes the unit; `build_snapshot` checks the answer's `unit` matches,
  because nothing below it reads that field and a substitution would therefore be invisible.
  An occupancy is not approximately transferable between units — it sets the reserve price,
  contention, scarcity and every budget factor, so the wrong unit's produces a full ladder of
  internally consistent numbers about the wrong ward.
- **A renamed field is not an absent input.** Readers of `HospitalState` take it typed and
  access fields directly (C-6). `getattr(state, name, None)` made a rename indistinguishable
  from missing data: every patient in every auction would score the signal absent, with only
  an unexplained coverage figure to show for it.
- **A threshold expressed as a fraction is not scale-free** (C-9). 0.85 of 20 beds and 0.85 of
  6 are different clinical situations, and on a small enough unit a fractional band can contain
  no achievable occupancy at all. Before reusing any `*_occupancy`/`onset` constant across the
  family, check what it means in beds for the smallest unit that will read it.
