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

## Training and evaluation

The learning loop in this repo is the Q-learning path: fit a value function from persisted
transitions, then serve the resulting policy through the same auction runtime.

```bash
python scripts/train_q.py --agent er
```

The offline learner reads the saved transition dataset, checks coverage and convergence, and
writes the learned weights artifact for the agent policy. The runtime then consumes the same
policy format through the allocation engine and the standard CLI/API entrypoints.

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
| `train_q.py` | offline Q-learning fit on the persisted transition dataset |
| `evaluate_er.py` | held-out paired comparison for the shipped policy artifact |
| `export_input_csv.py`, `export_output_csv.py` | dataset export helpers for validation and offline analysis |

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


