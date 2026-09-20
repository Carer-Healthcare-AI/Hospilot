# Bed and Diagnostic Allocation Q-Learning Model

This folder contains the HOSPILOT allocation auction and the Q-learning policies that bid in
it. Two resource families are served. A bed auction allocates one bed to one department. A
diagnostic auction allocates one capacity interval on one machine, against a deadline.

| Family | Resource | Registered resources |
|---|---|---|
| `bed` | One bed | `icu`, `hdu`, `pacu`, `resus`, `ed`, `ward` |
| `diagnostic_machine` | One capacity interval on one machine | `ct`, `mri`, `x_ray`, `ultrasound` |

The model observes the current patient, resource, auction, and hospital state, then chooses an
allocation strategy and bid level. It estimates one value for every action:

```text
Q(state, action) = action_weights . state + action_bias
```

It also learns an aggression value, `alpha`, between 0 and 1. When the model decides to
compete, `alpha` controls how much of the remaining bid range to use.

```text
alpha(state) = sigmoid(alpha_weights . state + alpha_bias)
```

The bed policy is agent-generic and can be used for any registered bidder: `er`, `ot`, `ward`,
`icu`, or `ambulance`. The diagnostic policy bids for `er`, `ot`, `icu`, and on CT also
`appointments`, the scheduled-outpatient bidder.

> **SYNTHETIC HOSPITAL — SIMULATION ONLY — NOT CLINICALLY VALIDATED.**
> Caps, budgets and reward weights are chosen, not fitted. `mode: live` is refused over HTTP.

## Actions

Each family has its own six-action space. They differ where the resource differs: a bed is
held, a machine is booked for an interval.

| Bed action | What it does |
|---|---|
| `win_now` | Bid to obtain the bed now. |
| `continue` | Remain in the auction and continue bidding. |
| `withdraw_alternative` | Use an available alternative unit. |
| `await_next_resource` | Wait for an expected bed release. |
| `re_enter_later` | Leave the current auction and re-enter when the trigger is met. |
| `withdraw_unplanned` | Leave without a planned pathway. |

| Diagnostic action | What it does |
|---|---|
| `win_now` | Bid to obtain the next capacity interval now. |
| `continue` | Remain in the auction and continue bidding. |
| `use_alternative` | Divert to a different modality that answers the same question. |
| `await_next_capacity` | Wait for the next interval on this machine. |
| `re_enter_later` | Leave the current auction and re-enter when the trigger is met. |
| `withdraw_unplanned` | Leave without a planned pathway. |

The model scores the actions that are available for the current patient and selects the action
with the highest Q-value. Eligibility is a hard filter, applied before scoring: a diagnostic
request whose modality, capability or deadline the machines cannot meet is removed from the
auction rather than penalised inside it.

## State features

Each encoder converts one decision into 25 values in the range `[0, 1]`. The two feature sets
are separate and are not interchangeable.

| Bed group | Features |
|---|---|
| Bid position | Utility, ceiling, headroom, standing bid, leader bid, distance from leader, leading status, heuristic alpha logit |
| Competition | Number of bidders, contention, round index, rounds remaining |
| Budget and time | Remaining budget, burn rate, elapsed shift time |
| Hospital state | Occupancy and boarding |
| Pathways | Safe-wait window, alternative hold, release probability, ETA, and known-value flags |

| Diagnostic group | Features |
|---|---|
| Clinical | Diagnostic value, urgency, delay pressure, operational impact |
| Resource | Machine scarcity, resource stress, transport burden |
| Bid position | Scaled utility and ceiling, own and leader bid over ceiling, leading status, distance behind, headroom fraction |
| Competition | Rounds remaining, scaled bidder count, scaled contention |
| Budget and time | Remaining budget fraction, burn rate |
| Pathways | Safe-wait fraction, next-capacity slack, alternative availability and yield ratio, scheduled-demand flag |

Each encoder carries a content-hash version, and a policy records the version it was fitted
under. Loading a policy whose version does not match the running build is refused rather than
reinterpreted, because the same position in the vector would mean a different quantity.

## How a decision is made

```text
patient + auction + budget + hospital state
                    |
             25-feature state
                    |
          available-action mask
                    |
             Q-value scoring
                    |
          highest-value action
                    |
       bid aggression or pathway
                    |
            auction decision
```

For a bidding action, the proposed increment is:

```text
increment = alpha * (bid ceiling - current bid)
```

The auction engine then applies the available budget and bid ceiling before submitting the
bid. Every round records one row per agent, including losers and withdrawals.

## How the model is trained

Training uses transitions recorded from seeded allocation simulations. Each transition
contains:

```text
state, action, reward, next state, available next actions, terminal flag
```

One episode represents one bidder during one shift. Rewards from the auctions in that shift
are combined into the episode return.

The Q-learning target is:

```text
target = scaled reward                                      for a terminal transition
target = scaled reward + gamma * Q(next state, best action) otherwise
```

During training, the learner:

1. Loads complete transitions for the selected bidder.
2. Splits the data by shift into training and validation sets.
3. Samples transition batches from replay memory.
4. Calculates the next feasible action with the current Q-values.
5. Calculates its value using the target Q-values.
6. Updates the selected action's weights from the TD error.
7. Updates the bid-aggression weights from observed bids.
8. Saves the fitted weights as JSON.

## Train a model

**Training is not shipped in this package.** This folder serves policies; it does not fit
them. The collection, training, baseline and evaluation harnesses live in the research tree,
and are deliberately excluded here along with `scripts/`.

To run the engine itself, install the project from the `bidding/` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[api,dev]"
```

A newly fitted artifact must record the encoder version of the build that will serve it. An
artifact fitted against a different encoder is refused at load time.

## Evaluation and AER

Average Episode Reward (AER) is the mean discounted reward across complete shift episodes:

```text
episode return = sum(gamma^t * reward_t)
AER            = mean(episode returns)
```

The preregistered comparison is against a fixed non-RL BASELINE — a ridge fit to realised
return-to-go — and not against the heuristic, which is excluded from the run by construction.
A run passes only if it clears both gates: the lower bound of the seed-clustered 95 per cent
confidence interval for `(Q - BASELINE)` is above zero, **and** the safety gate shows no
increase in abandonment or expiry, no mask violations and no non-finite values.

Reported results for the two families, from the research tree:

| Family | Q − BASELINE | 95% CI | Primary | Safety |
|---|---:|---|---|---|
| Bed | +82.18 | [61.05, 104.12] | pass | pass |
| Diagnostic | +1.99 | [1.78, 2.21] | pass | fail — abandonment +17 |

Neither artifact in `../artifacts/model/` is the artifact those runs measured, so no
improvement claim is made from them here.

## Use the trained model

Both families run the deterministic heuristic by default. That is what the CLI and the HTTP
routes below serve.

Run one bed auction:

```powershell
python -m allocation "ER, OT, and ICU/Ward demand compete for one limited ICU bed"
```

Run one diagnostic allocation:

```powershell
python -m allocation.use_cases.diagnostic_machine scenarios
python -m allocation.use_cases.diagnostic_machine run three_way_contention
python -m allocation.use_cases.diagnostic_machine governance
```

Start the HTTP API:

```powershell
python -m allocation.api
```

| Route | Purpose |
|---|---|
| `GET /health` | Liveness and the versions this process is running. |
| `GET /use-cases` | Registered bed profiles, and a query that resolves to each. |
| `GET /scenarios` | Bed scenario names. |
| `POST /auction` | Run one bed auction and return the full bid ladder. |
| `POST /session` | Run many bed auctions against one ledger; shows burn rate. |
| `GET /diagnostic/modalities` | The four modalities and their caps and budget tables. |
| `GET /diagnostic/scenarios` | Deterministic diagnostic fixtures. |
| `POST /diagnostic/auction` | Run one diagnostic auction and return the full bid ladder. |

Request one diagnostic auction from a fixture:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/diagnostic/auction `
  -H "content-type: application/json" `
  -d "{\"scenario\":\"three_way_contention\"}"
```

`POST /diagnostic/auction` names the world exactly one way: `scenario`, `query`, or
`machines` with `requests`. The last carries patient data and is refused unless the process
was started with `ALLOCATION_API_KEY` set, the same rule `candidates` follows on
`POST /auction`. No clinical value is defaulted; a missing one is refused and named.

To serve a Q policy, the process is started with a policy path, and the artifact's encoder
version must match the running build. The two artifacts in `../artifacts/model/` were fitted
under earlier encoders and are refused by this build; serving a policy here requires refitting
against the current encoder.

## Main files

| File | Purpose |
|---|---|
| `allocation/contracts.py` | Frozen types that cross layer boundaries. |
| `allocation/rl/encoder.py` | Defines the bed state features and action space. |
| `allocation/rl/policy.py` | Loads bed weights and produces auction decisions. |
| `allocation/profiles/registry.py` | Generic resource-profile machinery; no use case. |
| `allocation/use_cases/bed/profiles/` | The six bed profiles. |
| `allocation/use_cases/diagnostic_machine/encoder.py` | Defines the diagnostic state features. |
| `allocation/use_cases/diagnostic_machine/policy.py` | The deterministic diagnostic bidder. |
| `allocation/use_cases/diagnostic_machine/serving.py` | Loads diagnostic weights and serves them. |
| `allocation/use_cases/diagnostic_machine/auction.py` | One diagnostic auction, on the shared core. |
| `allocation/api/app.py` | HTTP routes for both families. |
| `allocation/api/diagnostic.py` | Service functions behind `/diagnostic/*`. |
| `allocation/config/reward.yaml` | Bed reward values and discount factor. |
| `allocation/config/` | Shared tables: thresholds, auction, rules, bed caps and budgets. |
| `allocation/use_cases/diagnostic_machine/config/` | Per-modality caps, budgets, pathway and reward. |
