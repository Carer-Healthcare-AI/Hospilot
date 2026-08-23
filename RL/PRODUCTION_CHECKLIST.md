# Production Readiness — Review Log & Checklist

**Reviewed:** 2026-08-11 · full pass over the design (RL-Steps.md as normative, with
BUILD_SPEC / AGENT_BUDGET / RL_READINESS as the bridge) and the code, layer by layer.
Tick items as they land; add the date and commit next to each tick.

---

## Verdict

The **mechanism code is genuinely strong**. Layering is enforced (`contracts.py` imports
nothing, components never see caps, policy is a protocol), config is content-hash versioned,
absence-is-not-zero is structural, and the audit layer refuses unusable rows. The design docs
already catalogue the *data/model* gaps exhaustively (F-01…F-31). What's missing is the
**operational shell around the engine** — the things that make it runnable, observable, and
safe as a service rather than a library. That is where the work is.

---

## A · Engineering gaps (code-level, not in the flag list)

Priority order. These are new findings on top of BUILD_SPEC §9.

- [ ] **A1 · Version control + CI.** Not a git repository. `git init`, first commit, then CI
      running pytest + ruff + mypy (all already configured in `pyproject.toml`, nothing runs
      them automatically). *"Maintainable" starts here.*
- [ ] **A2 · Logging.** Zero `logging` imports in `allocation/`. The loader docstring claims
      unsigned rules are "logged at auction open" — they only reach the trace/audit rows.
      Add structured logs at: auction open/close (auction_id, mode, unsigned-rule count),
      settlement, guard clamps, snapshot reads, sink writes. ~Half a day, highest
      maintainability return.
- [ ] **A3 · Reward pipeline persistence + scheduler.** `PendingObservation` claims to be
      "persisted, not held in memory" but no store exists and nothing wakes at `due_at` to
      call `score()`. A restart loses every pending 4-hour window, unrecoverably. Needs a
      `pending_observation` table + batch job. Also `ObservationSource` has **no
      implementation** — the 8 computable reward terms have schema sources but no reader.
- [ ] **A4 · Wire `PostgresSink`.** No driver, no DSN config, no migration runner. Apply
      091 + **093** to a real Postgres. **F-18 is time-critical:** every day 093 isn't
      collecting is a day the 30-day Demand clock hasn't started.
- [ ] **A5 · Real `DataSource` (Hasura/DB reader).** Only the Appendix C fixture exists.
      Single largest missing code artefact. Related: **nothing subscribes to
      `change_queue`** — the production trigger (bed-release → `opens_at()` →
      `run_auction`) has no scheduler/subscriber.
- [ ] **A6 · Async run path + failure semantics.** `run_allocation` is sync;
      `build_snapshot_sync` calls `asyncio.run()` per round. `asyncio.gather` has no
      `return_exceptions` — one failed patient read kills the whole snapshot. Decide: proceed
      with that candidate's signals absent (coverage machinery supports it) or abort. Add
      timeouts + retries for the real DataSource.
- [ ] **A7 · Budget ledger concurrency.** Budgets are in-memory dataclasses; `settle` isn't
      atomic against storage. Two simultaneous auctions race the same department budget.
      When the ledger moves to `allocation.agent_budget`: row locking
      (`SELECT … FOR UPDATE`) or optimistic versioning. Also: `RunStore` is a plain
      `OrderedDict` mutated from FastAPI's threadpool — add a `threading.Lock`.
- [ ] **A8 · Timezone convention.** Mixed: `datetime.now(timezone.utc)` in engine/writer,
      `datetime.now(tz=None).astimezone()` in observer, local-time shift resolution. Pick one
      rule (aware-UTC everywhere; hospital-local only at shift-boundary resolution) and
      enforce it — a naive/aware mix corrupts shift budget rows silently (F-17's failure
      mode via a different door).
- [ ] **A9 · Stamp code version in audit rows.** `caps_version`/`config_version` are stamped;
      a *code* change (scoring bug fix) alters numbers with no version change on the row.
      Add package version or git SHA to `AuditBundle`. Cheap now, impossible retroactively.
- [ ] **A10 · Safety-constraint evaluator.** `guards.safety_violations` raises
      `NotImplementedError` if constraints are ever declared — the day clinicians hand over
      the list (F-13), it can't be turned on. Build the evaluator ahead of sign-off, against
      placeholder rules kept `undeclared`.

---

## B · Needed from others — start these conversations now (long-lead)

- [ ] **B1 · Hospilot sync owner:** sign-off on migration 092 (`vitals.on_oxygen` + O₂ mode)
      → NEWS2 7/7, CB .20, UR .30 (F-04). Note **F-30**: the documented pharmacy-order
      fallback was never implemented — against real data this input is fully absent.
- [ ] **B2 · Clinical records / EHR:** structured **mortality/disposition field** (F-01) →
      blocks 100% of RL training. The single hardest blocker.
- [ ] **B3 · Clinical governance:** `safety_constraints` (currently `[]` — *nothing*
      enforced), ICU-benefit table, safe-hold durations, reward term values, resolve F-22's
      190-vs-200 arithmetic.
- [ ] **B4 · Medical director / governance:** `n_win`/`n_req` targets; does ICU bid (F-12);
      emergency-bypass and refund-on-release rules (the "win then hand back" free option).
- [ ] **B5 · Finance:** cost table — cancellation / boarding / escalation (FI .30, B.6).
- [ ] **B6 · Pharmacy:** drug→class table review (F-14).
- [ ] **B7 · Operations:** shift label → clock-time boundaries (F-17) — on the critical path
      for the per-shift budget recompute.
- [ ] **B8 · OT team:** PACU representation in `ot_room_status` (F-05) — OT's entire
      Waiting/Delay term.

---

## C · Models / values to fit (data-science track, parallel)

- [ ] **C1 · Budget scale (F-27) — the RL blocker people will miss.** At Base 700 burn is
      0.6–18.6%; the budget constrains nothing, so any learner correctly discovers "bid
      ceiling every time." Fitting `common_points` from observed burn is a *prerequisite*
      for RL, not a tuning pass.
- [ ] **C2 · B.9 / B.5 / B.8** — buildable **today** from existing `vitals`/`labs` via
      retrospective extract (self-supervised; recipe in RL_READINESS §7.6.1). The interim
      uplift double-counts the NEWS2 slope (F-29) and is the only unsigned assumption that
      can *reallocate* a bed — prioritise its fitted replacement.
- [ ] **C3 · Freeze before any `policy/rl.py`:** per-agent vs global reward (F-23 —
      different algorithms, not constants) and the state vector + normalisation (F-24 —
      versioned like caps).

---

## D · Suggested order

1. [ ] **This week, no dependencies:** A1 git+CI · A2 logging · A9 version stamping ·
       A7 RunStore lock · A8 timezone rule · A6 async/failure semantics.
2. [ ] **Backend:** Postgres up, apply 091 + **093 (starts the 30-day clock)**, wire
       PostgresSink (A4), pending-observation store + observer job (A3).
3. [ ] **Integration:** Hasura DataSource (read-only first) → change_queue subscriber +
       auction scheduler (A5) → safety evaluator built ahead of sign-off (A10).
4. [ ] **In parallel, people tasks:** all of section B — the long poles.
5. [ ] **Data science:** C2 retrospective extract → replace interim uplift → B.5/B.8.
6. [ ] **Only then RL:** C1 fit budget scale → C3 freeze encoding + reward architecture →
       `policy/rl.py` behind the existing protocol, evaluated against the tuned heuristic on
       paired seeds.

---

## Done

- [x] **2026-08-11 · ward_crash.yaml doc-rot fixed** — header claimed the pre-fix F-25
      behaviour ("still cannot afford to win"); now states F-25 RESOLVED (Ward wins ~95
      under common Base) and names the two pinning tests.
