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

## How the RL was done

The reported experiment trains only the ER bidder; OT and Ward remain on the frozen heuristic.
This keeps the environment stationary enough to attribute a change in reward to the ER policy.
For each auction, the policy receives the 22-feature state vector described above, masks actions
that are not feasible, chooses one of the six actions, and produces a bid-aggression value.

One episode is one agent over one shift. Each auction contributes an outcome reward after the
configured four-hour observation window. The episode return is:

```
G = sum(gamma^t * reward_t), where gamma = 0.99
AER = mean G over complete ER shift episodes
```

An episode with any unobserved reward term is excluded instead of being treated as a zero-reward
episode. In the simulator, all required outcomes are generated so complete episodes can be used
for training and comparison.

### Training the published ER policy

The shipped [ER policy](artifacts/er_policy.D_672ev_pop48.json) was fitted with the cross-entropy
method (CEM), a derivative-free policy search implemented in [rl/train.py](allocation/rl/train.py).
It is a linear policy with 161 fitted parameters: six action weight rows and biases plus the
bid-aggression head.

Training proceeded as follows:

1. Sample a population of complete policy parameter vectors.
2. Evaluate every candidate on the same simulated arrival streams.
3. Rank candidates with unplanned abandonment behind every feasible candidate, then rank by AER.
4. Keep the best 25%, refit the sampling mean and spread, and repeat.
5. Save weights with their encoder and simulator-fabrication hashes so incompatible artifacts are
   refused at load time.

The published run used:

| setting | value |
|---|---:|
| Learning agent | ER |
| Population | 48 policies per generation |
| Generations | 14 |
| Candidate evaluations | 672 |
| Training worlds | seeds 11–18 |
| Shifts per world | 4 |
| CEM random seed | 0 |
| Base budget | 120 utility points |
| Simulated release/candidate rate | 1.8 / 3.6 per hour |

Run the same full configuration under a new artifact label:

```bash
python scripts/scale_cem.py --population 48 --generations 14 \
  --sigma-floor 0.05 --label repro_D_672ev_pop48
```

This is a long run. For a small pipeline smoke test, `python scripts/train_er.py` uses only six
generations, population 16, and two training seeds; it does not reproduce the published model.

The repository also contains offline and simulator-collected temporal-difference Q-learning
(`train_q.py` and `train_q_online.py`) and PPO experiments. Those are separate experiments and
did not produce `er_policy.D_672ev_pop48.json`.

### Evaluation

Evaluation is paired: the heuristic and learned policy see identical seeds, arrivals, patient
trajectories, and shifts. AER differences are calculated shift by shift, and the reported
`t = mean(paired differences) / standard error`. This pairing removes much of the simulator's
large between-shift variance.

The seed ranges have distinct roles:

- **11–18:** CEM training; never use these to claim generalisation.
- **101–200:** validation/model selection. These worlds were unseen during fitting, but several
  candidate configurations were compared on them, so this is not an untouched final test set.
- **201–204:** reserved for fabrication-sensitivity checks.
- **301–400:** confirmation range, evaluated after selecting the policy.

Routine evaluation runs the paired comparison, fabrication sweep, and shadow-safety check:

```bash
python scripts/evaluate_er.py --weights artifacts/er_policy.D_672ev_pop48.json
```

The routine command uses 24 comparison seeds. Use these for the recorded 100-seed comparisons:

```bash
# Validation/model-selection range
python scripts/resolve_comparison.py 100 \
  --weights artifacts/er_policy.D_672ev_pop48.json --seed-start 101

# Untouched confirmation range; also preserves every paired shift in CSV
python scripts/export_validation.py 100 \
  --weights artifacts/er_policy.D_672ev_pop48.json --seed-start 301 \
  --tag D_672ev_pop48.confirm301

# Full nine-metric report against the shipped heuristic
python scripts/scorecard.py --weights artifacts/er_policy.D_672ev_pop48.json
```

### ER/AER results

On the 100-world validation range (seeds 101–200), the two policies shared 689 complete ER
shift episodes:

| metric | heuristic | published ER policy |
|---|---:|---:|
| Average Episode Reward (AER) | 713.93 | **782.25** |
| Relative AER change | — | **+9.6%** |
| Paired t-ratio | — | **5.08** |
| Better shifts | — | 390 / 689 |
| Allocation efficiency | 79.2% | 83.7% |
| Beds unallocated | 6.4% | 7.5% |
| Burn rate | 53.8% | 50.2% |
| Unplanned abandonments | 0 | 0 |

The separate confirmation range (seeds 301–400) reported model AER **785.56**, **+10.0%**
against the heuristic, with `t = 5.32`. The learned policy was better on 371 / 689 shifts
(53.8%; sign-test `p = 0.044`). The magnitude improvement is therefore driven partly by larger
gains on a subset of shifts, not uniform improvement on every shift.

These are simulator results, not evidence of improved patient outcomes. The simulator's arrival
process, deterioration trajectories, and outcome model are fabricated, and the reward points
still await clinical sign-off. The result only says that this policy paced the simulated ER
budget better than the shipped heuristic under the evaluated worlds.

### Tests

The automated suite covers feature encoding, action masking, policy loading/version refusal,
reward and episode construction, Q-learning/PPO mechanics, paired evaluation, shadow safety,
auction invariants, API behavior, and scenario regressions.

```bash
python -m pytest
python -m pytest tests/test_qlearn.py tests/test_ppo.py tests/test_api_policy.py
```

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
| `train_er.py` | small CEM training smoke run |
| `scale_cem.py` | full CEM fit plus a 100-seed paired comparison |
| `evaluate_er.py` | routine paired comparison, fabrication sweep, and shadow check |
| `resolve_comparison.py` | configurable full-size paired comparison |
| `export_validation.py` | paired per-shift results and aggregate statistics |
| `scorecard.py` | nine-metric comparison against the heuristic |
| `train_q.py`, `train_q_online.py` | offline and simulator-collected TD Q-learning experiments |
| `train_ppo.py` | PPO experiment with held-out probes |
| `export_input_csv.py`, `export_output_csv.py` | validation and offline-analysis CSV helpers |

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
