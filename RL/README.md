# HOSPILOT — Allocation Auction

A scarce hospital bed is allocated by an auction between departments. Each department is an
agent with a utility-points budget. Every candidate patient is scored into a utility, capped
into a bid ceiling, and bid for across three rounds. The winner takes the bed, losers pay a
participation charge, and budgets carry across the shift.

A learned policy — linear Q over six actions, plus an aggression head — can bid in place of the
built-in heuristic. One trained model ships with the repo.

```bash
pip install -e ".[dev]"
python -m allocation
python -m pytest          # 581 tests
```

Python ≥ 3.11. The engine's only runtime dependency is PyYAML.

---

## The auction

- **Bidders:** `er`, `ot`, `ward`.
- **Utility:** 8 components — clinical benefit, urgency, waiting, throughput, operational,
  financial, alternative, resource stress.
- **Rounds:** 3 × 120 s. Round 1 sealed, later rounds open and leader-first. Utilities are
  re-scored each round, so a deteriorating patient's bid rises mid-auction.
- **Ceiling:** `Ceiling = U × (1 + uplift)`.
- **Budget:** `B = Base × Demand × Fairness × Scarcity`, recomputed per shift. Deduction at
  commitment rate 0.25. Burn rate is the health metric; working band 0.70–1.10.
- **Guards:** ceiling, affordability and whole-point clamps applied *after* the policy speaks.
- **Reward:** one scalar per auction, observed 4 h after close, γ = 0.99, episode = shift.
- **Audit:** one row per agent per round, losers and withdrawals included, with the full
  component breakdown. A row missing losers, breakdown, coverage or versions is refused.
- **Modes:** `live`, `simulation`, `advisory`, `replay`. Defaults to `simulation`; `live` is
  refused against the fixture data source.

Six bed profiles are registered (icu, hdu, pacu, resus, ed, ward). All numbers live in YAML
under [allocation/config/](allocation/config/), loaded through one typed accessor that hashes
the files into `caps_version` / `config_version`.

## The learned policy

```
Q(s, a)  =  w_a · s + b_a          one weight row per action
alpha(s) =  sigmoid(v · s + c)     how hard to bid, when the choice is to bid
```

**Six actions:** `win_now`, `continue`, `withdraw_alternative`, `await_next_resource`,
`re_enter_later`, `withdraw_unplanned`. Infeasible actions are masked, not penalised.

**22-feature state vector** ([rl/encoder.py](allocation/rl/encoder.py), version `96ceb154f5fd`) —
bid position, competition, budget, hospital, patient. All normalised to [0, 1]; absence is a
value plus a `*_known` flag, never a zero. Weights fitted under a different encoder version are
refused at load.

**Serving is shadow-first.** `--policy` loads the weights and lets them decide nothing: the
heuristic allocates, the learned choices are logged. Acting needs `--live-policy`, which refuses
to run while `auction.yaml`'s `safety_constraints` is empty.

## The model

`artifacts/er_policy.D_672ev_pop48.json` — ER bidding policy, format `rl-linear-v1`.

Fitted by cross-entropy method: population 48 × 14 generations = 672 evaluations, training seeds
11–18. Scored on **100 held-out seeds (101–200), 689 paired shifts**, identical arrival streams:

| | heuristic | model |
|---|---|---|
| Average episode reward | 713.93 | **782.25** (+9.6%, t = 5.08) |
| Better on | — | 390 / 689 shifts |
| Allocation efficiency | 79.2% | 83.7% |
| Beds unallocated | 6.4% | 7.5% |
| Burn rate | 53.8% | 50.2% |
| Abandonments | 0 | 0 |

```bash
python -m allocation --policy artifacts/er_policy.D_672ev_pop48.json
python scripts/evaluate_er.py --weights artifacts/er_policy.D_672ev_pop48.json
```

Those numbers are a paired comparison against the heuristic inside a seeded simulator whose
arrival process, deterioration trajectories and outcome model are invented. They say which
policy paces a budget better. They say nothing about patient outcomes.

## CLI

```bash
python -m allocation                                    # one auction, step by step
python -m allocation --json | --explain
python -m allocation --scenario scenarios/ward_crash.yaml
python -m allocation --events 12 --every 45m            # session, budgets across shifts
python -m allocation --copy-config ./my-config          # then --config-dir ./my-config
```

Other flags: `--mode`, `--at`, `--rounds`, `--no-uplift`, `--policy`, `--live-policy`.

## HTTP API

```bash
pip install -e ".[api]"
python -m allocation.api            # 127.0.0.1:8000
```

```
GET  /health · /config · /use-cases · /scenarios · /auctions
POST /auction · /session                        ?format=text for the rendered report
GET  /auction/{id} · /audit · /explain · /derivation
```

Env vars: `ALLOCATION_HOST`, `ALLOCATION_PORT`, `ALLOCATION_API_KEY`, `ALLOCATION_STORE_SIZE`,
`ALLOCATION_SCENARIO_DIR`. Setting a key requires `X-API-Key` everywhere except `/health`.
One worker — runs are held in process memory.

[examples/patients.json](examples/patients.json) is a ready `POST /auction` body.

```bash
docker compose up --build
```

Two-stage build, non-root, read-only filesystem, bound to `127.0.0.1`, 1 CPU / 512 MB.

## Simulator and scripts

[allocation/sim/](allocation/sim/) implements the same `DataSource` seam a real reader will, so
every layer above `ingest/` runs unmodified. Time advances between calls, everything is seeded
per patient so paired runs are reproducible, and occupancy responds to allocation.
[sim/fabricated.py](allocation/sim/fabricated.py) enumerates every invented constant and hashes
them into the `fabrication_version` stamped on each trained artifact.

| script | does |
|---|---|
| `train_er.py` | CEM policy search for ER |
| `run_training.py` | same run, progress flushed to a file line by line |
| `evaluate_er.py` | held-out paired comparison, fabrication sweep, shadow check |
| `train_q_online.py` | TD learning under ε-greedy exploration, ε 0.60 → 0.05 |
| `train_ppo.py` | one PPO seed per invocation (needs `numpy`, `scipy`) |
| `train_q.py`, `export_input_csv.py`, `export_output_csv.py` | read a transition corpus / validation CSV that is not published here |

## Layout

```
allocation/   contracts · cli · profiles · config · ingest · features · utility · budget
              auction · policy · pathway · reward · rl · sim · trigger · audit · api
scripts/      trainers, evaluation, CSV export
tests/        21 modules, 581 tests
db/migrations 091 allocation schema · 092 vitals oxygen flags · 093 forecast retention
              094 the decision behind the bid
scenarios/    ward_crash.yaml, step_down.yaml
artifacts/    the published policy
```

## Documents

[RL-Steps.md](RL-Steps.md), [RL_STEPS_END_TO_END.md](RL_STEPS_END_TO_END.md) and
[AGENT_BUDGET.md](AGENT_BUDGET.md) are the framework — normative; no formula or mechanism is
changed in code. [BUILD_SPEC.md](BUILD_SPEC.md) is the bridge and holds the `B.n` / `F-n`
identifiers cited throughout the source. Also: [RL_READINESS.md](RL_READINESS.md),
[RL_TRAIN_VALIDATE_INFER.md](RL_TRAIN_VALIDATE_INFER.md),
[RL_EVAL_CHECKLIST.md](RL_EVAL_CHECKLIST.md), [RL_METRIC.md](RL_METRIC.md),
[VALIDATION_EXPLAINED.md](VALIDATION_EXPLAINED.md),
[RL_EXPERIMENTS_LOG.md](RL_EXPERIMENTS_LOG.md), [RL_FIXES.md](RL_FIXES.md),
[PPO_EXPERIMENT_PLAN.md](PPO_EXPERIMENT_PLAN.md),
[PPO_DIAGNOSTIC_HANDOVER.md](PPO_DIAGNOSTIC_HANDOVER.md),
[INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md), [BACKEND_HANDOVER.md](BACKEND_HANDOVER.md),
[LIVE_FLOW.md](LIVE_FLOW.md), [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md).

## Unsigned and fabricated

Stated here because the code states it at runtime.

- `Config.unsigned` names every rule table still on assumed values — a default run reports 12.
- Reward point values are RL-Steps' own and have never been fitted.
- `caps_icu_bed.yaml` is the only caps file chosen for its resource; the other five are copies.
- `auction.yaml`'s `safety_constraints` is empty and marked `undeclared`.
- The simulator's arrival process, deterioration trajectories and outcome model are invented.
- `no_mortality` has no structured source, so scored episodes are marked incomplete.
- The shipped `DataSource` serves three invented fixture patients.
