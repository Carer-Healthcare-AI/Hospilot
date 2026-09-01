# Bed Allocation Q-Learning Model

This README covers only the temporal-difference Q-learning model implemented in this `RL/`
folder for the hospital bed-allocation auction. The CEM and PPO experiments use some of the
same policy and simulator code, but they are not the model documented here.

## Model identity

| Item | Value |
|---|---|
| Model family | `rl-linear-v1` |
| Model type | Linear temporal-difference Q-learning with Double-Q targets |
| Learning implementation | `allocation.rl.qlearn.QLearner` |
| Python serving class | `allocation.rl.policy.LinearQPolicy` |
| Learning agent in recorded runs | `er` |
| Other bidders during training | Frozen heuristic (`ot`, `ward`) |
| State size | 22 normalized features |
| Action count | 6 discrete actions |
| Parameter count | 161, including the bid-aggression head |
| Encoder version | `96ceb154f5fd` |
| Simulator/fabrication version | `f14a17eef7b1` |
| Offline output path | `artifacts/er_q_policy.json` |
| Online output path | `artifacts/er_q_policy.online.json` |
| Committed TD-trained artifact | None |
| Current evidence status | `EXPERIMENTAL_REJECTED_NOT_SERVED` |

> **Important:** this repository does not currently commit or serve an accepted Q-learning
> artifact. The only JSON weights committed under `artifacts/` were fitted by CEM, not by TD
> Q-learning. The recorded offline and online Q-learning runs both failed the policy and safety
> gates described below. Their results are retained as experimental evidence and must not be
> presented as production or clinical performance.

## What the model decides

For each candidate in each auction round, the model scores six patient-level strategies:

| Action | Meaning in the bed auction |
|---|---|
| `win_now` | Continue bidding aggressively because immediate acquisition is preferred. |
| `continue` | Remain in the auction without selecting an exit pathway. |
| `withdraw_alternative` | Leave for the best available alternative unit. A named unit is required. |
| `await_next_resource` | Wait for a predicted bed release. An ETA and sufficient release probability are required. |
| `re_enter_later` | Leave temporarily under a monitored trigger that can reopen bidding. |
| `withdraw_unplanned` | Leave without an onward plan. This is always represented explicitly so abandonment can be measured. |

The policy is linear, not a neural network:

```text
Q(s, a)  = weights[a] . state + bias[a]
action   = argmax Q(s, a), over feasible actions only
alpha(s) = sigmoid(alpha_weights . state + alpha_bias)
```

`win_now` and `continue` compete for the bed and use `alpha` to decide how much remaining bid
headroom to expose. The four other actions exit the current auction and, except for
`withdraw_unplanned`, must carry a valid pathway plan.

## State representation

The encoder produces 22 values, all clipped to `[0, 1]`:

| Group | Features |
|---|---|
| Bid position | `utility`, `ceiling`, `headroom`, `standing_bid`, `leader_bid`, `behind_by`, `is_leading` |
| Competition | `n_bidders`, `contention`, `round_index`, `rounds_left` |
| Budget and time | `budget_remaining`, `burn_rate`, `shift_fraction_elapsed` |
| Hospital state | `occupancy`, `boarding` |
| Exit pathways | `safe_wait`, `safe_wait_known`, `alternative_hold`, `alternative_available`, `release_probability`, `release_known` |

Missing clinical or pathway values are represented by a value plus an explicit known/available
flag. They are not silently interpreted as a real zero. Feature order is load-bearing and is
hashed into the encoder version. `QWeights.load()` refuses weights when either the encoder
version or action ordering differs from the running code.

## How the model is used in bidding

The Q model selects a strategy and an aggression value. The auction engine still owns the
financial and safety mechanics:

```text
candidate + patient + hospital + auction + budget + pathways
                              |
                       22-state encoder
                              |
                    feasible-action mask
                              |
                    Q-value for each action
                              |
          compete --------------------------- planned/unplanned exit
             |                                          |
        alpha head                               pathway plan required
             |
increment = alpha * (ceiling - current bid)
             |
ceiling + affordability + whole-point guards
             |
         auction and settlement
```

The Q-value argmax never includes an action that is mechanically impossible in the current
state. Bid proposals are then clamped to the utility ceiling and affordable budget by guards
outside the learned policy.

Action coverage is checked during training, but the current artifact format does not carry an
`untrained_actions` mask. A feasible action whose row was never updated remains at `Q = 0` and
can win the argmax when trained actions are negative. This is the main failure observed in the
offline Q experiment.

Serving is shadow-first. A policy loaded with `--policy` is observed while the heuristic still
acts. `--live-policy` places the learned policy behind `SafetyGate` with the heuristic as a
fallback. The currently configured gates are provisional engineering rules, not clinically
signed-off rules.

## Reward, episodes, and Bellman update

One episode is one agent over one shift. Each auction becomes one transition with the state,
chosen action, reward, next state, next feasible actions, and a terminal flag. Transitions do
not bootstrap across shift boundaries because budgets reset at the next shift.

The configured reward is observed four hours after an auction and includes outcomes such as
transfer to ICU, stabilization, reduced boarding, released capacity, avoided cancellation,
deterioration, emergency escalation, throughput, and revenue. A missing outcome makes the
episode incomplete; it is dropped rather than imputed as zero. The simulator supplies invented
outcomes so training episodes can complete.

For each transition, the learner applies a feasible-action Double-Q target:

```text
scaled_reward = reward / reward_scale

target = scaled_reward                                      if terminal
target = scaled_reward + gamma * Q_target(next_state, a*)   otherwise

a*       = argmax Q_online(next_state, action), over next_feasible
TD error = target - Q_online(state, chosen_action)
```

Only the chosen action's weight row and bias are updated. The implementation uses:

- `gamma = 0.99`;
- reward scaling by the configured maximum reward (`200` for the current table);
- Huber clipping with `delta = 1.0`;
- online weights to select the next action and frozen target weights to value it;
- periodic target synchronization;
- replay sampling to break correlations between consecutive auctions.

The continuous `alpha` head is fitted separately with advantage-weighted regression over
observed non-exit bids. This avoids treating bid magnitude as another discrete Q action.

Average Episode Reward (AER) is the mean discounted return over complete ER shift episodes:

```text
episode_return = sum(gamma^t * reward_t)
AER            = mean(episode_return)
```

## How the Q-learning experiments were trained

Two training paths are implemented. They answer different questions and produce different
artifact names.

### 1. Offline fitted Q-learning

The offline path first generates a fixed, log-shaped transition corpus using the deterministic
heuristic:

```powershell
python scripts\build_dataset.py --seeds 40 --shifts 12
python scripts\train_q.py --agent er --epochs 400
```

Collection configuration:

| Setting | Recorded value |
|---|---:|
| World seeds | `7000` through `7039` |
| Worlds | 40 |
| Shifts per world | 12 |
| Behavior policy | Heuristic only, `epsilon = 0.0` |
| Total recorded transitions across agents | 19,919 |
| Base budget | 120 utility points |
| Simulated release/candidate rate | 1.8 / 3.6 per hour |
| Dataset path | `artifacts/transitions.jsonl` |

Fitting configuration:

| Setting | Value |
|---|---:|
| Initialization | All-zero Q and alpha weights |
| Epochs | 400 |
| Minibatch size | 128 |
| Learning rate | 0.02 |
| Train/holdout split | 75% / 25% by `(agent, shift_id)` |
| Target synchronization | Every 25 epochs |
| Huber delta | 1.0 |
| Double-Q | Enabled |
| Training replay seed | 0 |

The split preserves transition chains, but it has a known limitation: `shift_id` does not
contain the simulator seed. All 40 worlds contribute to the same calendar-slot keys, so this
holdout measures unseen time slots pooled across already-seen worlds, not generalization to new
worlds. Policy evaluation therefore uses completely disjoint seed ranges.

The heuristic corpus covered only two ER actions: `win_now` and `re_enter_later`. The other
feasible action rows stayed at zero. More heuristic-generated data cannot repair this because
the behavior policy remains deterministic.

### 2. Online collect-then-fit Q-learning

The online path alternates epsilon-greedy collection with replay updates:

```powershell
python scripts\train_q_online.py --agent er --rounds 12
```

| Setting | Recorded value |
|---|---:|
| Initialization | All-zero Q and alpha weights |
| Collection rounds | 12 |
| Seeds per round | 3 |
| Shifts per seed | 6 |
| Collection seeds | Formula `1000 + round*97 + offset`, spanning `1000`-`2069` |
| Epsilon schedule | 0.60 to 0.05, geometric decay |
| Replay capacity | 20,000 transitions |
| Final recorded buffer | 2,907 transitions |
| Updates per round | 40 minibatches |
| Minibatch size | 128 |
| Sampled transition updates | 61,440 |
| Learning rate | 0.02 |
| Output path | `artifacts/er_q_policy.online.json` |

Exploration samples only feasible actions and randomizes `alpha` when it explores a competing
action. ER learned five of the six action rows. `await_next_resource` received no updates
because the configured release-probability gate never made it feasible for the recorded ER
patients; this is reported as unavailable rather than an action-coverage failure.

## Evaluation method

The heuristic and Q policy are evaluated on identical simulated worlds. Returns are paired by
`(seed, shift_id)`, and the t-ratio is computed over shift-by-shift AER differences. The main
recorded gate uses:

- seeds `101` through `200`;
- six shifts per world;
- 689 paired, complete ER shift episodes;
- no overlap with offline seeds `7000`-`7039` or online seeds `1000`-`2069`.

This band was used for model comparison and is therefore a selection/gate band, not an
untouched final confirmation set.

Re-run a policy comparison after producing an artifact:

```powershell
python scripts\resolve_comparison.py 100 `
  --weights artifacts\er_q_policy.online.json `
  --seed-start 101

python scripts\scorecard.py `
  --weights artifacts\er_q_policy.online.json `
  --out artifacts\scorecard.Qonline.recheck.log
```

## Recorded AER and safety results

| Metric | Heuristic reference | Offline Q | Online Q |
|---|---:|---:|---:|
| Average Episode Reward | 713.93 | 217.07 | 648.84 |
| Relative AER change | - | **-69.6%** | **-9.1%** |
| Paired t-ratio | - | -26.74 | -4.55 |
| Allocation efficiency | 79.2% | 50.1% | 75.7% |
| Beds unallocated | 6.4% | 5.1% | 10.3% |
| Unplanned abandonments | 0 | **17** | **358** |
| Reference agreement | - | 65.0% | 26.0% |
| Policy change rate | - | not recorded here | 74.0% |
| Verdict | Reference | **Rejected** | **Rejected** |

### Offline-Q interpretation

- Relative held-out TD error fell from 44.6% to 17.5%, so the fitted values converged under the
  limited split.
- Absolute held-out TD error rose from 0.2242 to 0.5807 because bootstrapped target magnitude
  grew; relative error is the convergence criterion.
- Only two of six action rows were trained.
- Zero-valued untrained rows could defeat negative learned values in the greedy argmax.
- The result was 69.6% below the heuristic and introduced 17 unplanned abandonments.

### Online-Q interpretation

- Exploration fixed coverage: all five actions that became feasible received updates.
- Training TD error rose from 0.1318 to 0.4001 instead of falling and flattening.
- Final in-loop AER was 378.97, 45.0% below its collection-world heuristic baseline of 689.02.
- On the disjoint gate band it improved substantially over offline Q but remained 9.1% below
  the heuristic.
- It selected `withdraw_unplanned` frequently enough to produce 358 abandonments, a safety
  failure despite complete action coverage.

The two experiments show that action coverage was necessary but not sufficient. Offline Q
could not value most choices, while online Q learned them but still converged to unsafe behavior.
Neither model should be promoted, served by default, or quoted as a successful RL result.

All figures above are simulator results. The simulator's arrivals, deterioration trajectories,
and outcome model are fabricated, and the reward point values await clinical sign-off. A future
positive simulator result would still require an untouched confirmation band, local-data
validation, safety review, and prospective shadow evaluation.

## Setup

From the `RL/` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[api,dev]"
```

Run the complete test suite:

```powershell
python -m pytest
```

Run the focused Q-learning and serving tests:

```powershell
python -m pytest tests\test_qlearn.py tests\test_q_actions.py tests\test_api_policy.py
```

## Training a new Q model

Do not overwrite an existing artifact when running a new experiment. Give every run a distinct
filename and record its dataset, encoder, fabrication version, seed ranges, and configuration.

### Offline candidate from the heuristic corpus

```powershell
python scripts\build_dataset.py --seeds 40 --shifts 12
python scripts\train_q.py `
  --data artifacts\transitions.jsonl `
  --agent er `
  --epochs 400 `
  --out artifacts\er_q_policy.offline_candidate.json
```

This run is expected to report incomplete action coverage unless the behavior corpus changes.

### Exploratory offline candidate

```powershell
python scripts\build_dataset.py `
  --seeds 40 --shifts 12 `
  --epsilon 0.30 `
  --explore-agent er `
  --out artifacts\transitions.eps30.candidate.jsonl

python scripts\train_q.py `
  --data artifacts\transitions.eps30.candidate.jsonl `
  --agent er `
  --epochs 400 `
  --out artifacts\er_q_policy.eps30_candidate.json
```

This is a simulator exploration study. It is not evidence about decisions clinicians would
make in real logged auctions.

### Online candidate

```powershell
python scripts\train_q_online.py `
  --agent er `
  --rounds 12 `
  --shifts 6 `
  --seeds-per-round 3 `
  --lr 0.02
```

The script writes `artifacts/er_q_policy.online.json`. Rename or archive it with a unique
experiment identifier before another run.

## Using a newly trained artifact

Because no accepted Q artifact is committed, the examples below require a file produced by one
of the training commands above.

### CLI: shadow mode

Shadow mode is the recommended first serving check. The heuristic allocates while the Q
decisions, gate refusals, and divergence are recorded:

```powershell
python -m allocation `
  --policy artifacts\er_q_policy.online.json
```

### CLI: acting in fixture simulation

```powershell
python -m allocation `
  --policy artifacts\er_q_policy.online.json `
  --live-policy
```

This runs behind provisional engineering gates with a heuristic fallback. It is only a fixture
simulation; the application refuses a real live allocation against its invented data source.

### HTTP API: shadow mode

```powershell
python -m allocation.api `
  --policy artifacts\er_q_policy.online.json
```

Inspect what was loaded:

```powershell
curl.exe -s http://127.0.0.1:8000/health
```

Request the RL path:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/auction `
  -H "content-type: application/json" `
  -d '{"policy":"rl"}'
```

Without `--live-policy`, the response's shadow section records the learned decision while the
heuristic remains the acting policy.

## Promotion requirements for a future artifact

A new candidate should not replace the current baseline unless all of the following are met:

1. Every feasible action has training coverage; never-feasible actions are explicitly reported.
2. Held-out relative TD error falls without unstable growth in Q or training TD error.
3. Paired AER is non-inferior or better on a preregistered validation band.
4. Allocation efficiency, unallocated-bed rate, budget burn, and abandonments pass separately;
   AER cannot trade away a safety failure.
5. A single selected candidate passes a new, untouched confirmation seed band.
6. Encoder, action order, configuration, fabrication, seeds, and artifact digest are recorded.
7. The artifact passes loader, CLI, API, shadow, safety-gate, and scenario tests.
8. Real deployment remains blocked until clinical governance and a prospective shadow period
   approve it.

## Source and evidence map

| Concern | File |
|---|---|
| State features and action order | `allocation/rl/encoder.py` |
| Q weights, loading, feasibility, and serving decisions | `allocation/rl/policy.py` |
| Replay, Bellman update, Double-Q target, coverage, offline and online fitting | `allocation/rl/qlearn.py` |
| Transition and trajectory construction | `allocation/sim/dataset.py` |
| Offline corpus generation | `scripts/build_dataset.py` |
| Offline trainer | `scripts/train_q.py` |
| Online collect-then-fit trainer | `scripts/train_q_online.py` |
| Reward definition | `allocation/config/reward.yaml` |
| Paired evaluation | `allocation/rl/evaluate.py`, `scripts/resolve_comparison.py` |
| Full metric report | `scripts/scorecard.py` |
| Recorded Q-run evidence and limitations | `scripts/export_data_provenance.py`, `scripts/dryrun_metrics.py` |
| Shadow policy, gates, and divergence monitor | `allocation/rl/pilot.py` |
| Bid increment and auction guards | `allocation/auction/rounds.py`, `allocation/auction/guards.py` |
| Provisional safety configuration | `allocation/config/auction.yaml` |
| Q-learning tests | `tests/test_qlearn.py`, `tests/test_q_actions.py` |
| CLI/API policy-serving tests | `tests/test_api_policy.py` |
