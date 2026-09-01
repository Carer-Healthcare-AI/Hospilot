# Bed Allocation Q-Learning Model

This folder contains a Q-learning model for the HOSPILOT bed-allocation auction. The model
observes the current patient, bed, auction, and hospital state, then chooses an allocation
strategy and bid level.

The policy implementation is agent-generic. It can be trained and used for any registered
bidder: `er`, `ot`, `ward`, or `icu`. Select the bidder with the training command's `--agent`
option.

The model estimates one value for every action:

```text
Q(state, action) = action_weights . state + action_bias
```

It also learns an aggression value, `alpha`, between 0 and 1. When the model decides to
compete for a bed, `alpha` controls how much of the remaining bid range to use.

```text
alpha(state) = sigmoid(alpha_weights . state + alpha_bias)
```

## Actions

| Action | What it does |
|---|---|
| `win_now` | Bid to obtain the bed now. |
| `continue` | Remain in the auction and continue bidding. |
| `withdraw_alternative` | Use an available alternative unit. |
| `await_next_resource` | Wait for an expected bed release. |
| `re_enter_later` | Leave the current auction and re-enter when the trigger is met. |
| `withdraw_unplanned` | Leave without a planned pathway. |

The model scores the actions that are available for the current patient and selects the action
with the highest Q-value.

## State features

The state encoder converts each decision into 22 values in the range `[0, 1]`.

| Group | Features |
|---|---|
| Bid position | Utility, ceiling, headroom, standing bid, leader bid, distance from leader, leading status |
| Competition | Number of bidders, contention, round number, rounds remaining |
| Budget and time | Remaining budget, burn rate, elapsed shift time |
| Hospital state | Occupancy and boarding |
| Pathways | Safe-wait window, alternative availability, release probability, and known-value flags |

## How a decision is made

```text
patient + auction + budget + hospital state
                    |
             22-feature state
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

The auction engine then applies the available budget and bid ceiling before submitting the bid.

## How the model is trained

Training uses transitions recorded from seeded bed-allocation simulations. Each transition
contains:

```text
state, action, reward, next state, available next actions, terminal flag
```

One episode represents one bidder during one shift. Rewards from the auctions in that shift are
combined into the episode return.

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

From the `RL/` directory, install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[api,dev]"
```

Choose one of the registered bidders: `er`, `ot`, `ward`, or `icu`. This example trains `er`:

```powershell
python scripts\build_dataset.py `
  --seeds 40 `
  --shifts 12 `
  --epsilon 0.30 `
  --explore-agent er `
  --out artifacts\transitions.er.jsonl

python scripts\train_q.py `
  --data artifacts\transitions.er.jsonl `
  --agent er `
  --epochs 400 `
  --lr 0.02
```

To train another bidder, use the same bidder name for `--explore-agent` and `--agent`, then give
the dataset and model their own output names.

## Evaluation and AER

Average Episode Reward (AER) is the mean discounted reward across complete shift episodes:

```text
episode return = sum(gamma^t * reward_t)
AER            = mean(episode returns)
```

The baseline and committed artifact are evaluated on the same simulated arrivals and shift
conditions. The recorded comparison used seeds `101` through `200` and produced 689 paired ER
shift episodes.

| Model | AER | Relative change |
|---|---:|---:|
| Baseline | 713.93 | - |
| Committed artifact | 782.25 | +9.6% |

Run the same paired comparison for a trained model:

```powershell
python scripts\resolve_comparison.py 100 `
  --weights artifacts\er_policy.D_672ev_pop48.json `
  --seed-start 101
```

## Use the trained model

Run one auction and display the Q-learning decisions alongside the baseline:

```powershell
python -m allocation --policy artifacts\er_q_policy.json
```

Use the Q-learning model to make the auction decisions:

```powershell
python -m allocation `
  --policy artifacts\er_q_policy.json `
  --live-policy
```

Start the HTTP API with the model loaded:

```powershell
python -m allocation.api `
  --policy artifacts\er_q_policy.json `
  --live-policy
```

Request an auction using the Q-learning policy:

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/auction `
  -H "content-type: application/json" `
  -d '{"policy":"rl"}'
```

## Main files

| File | Purpose |
|---|---|
| `allocation/rl/encoder.py` | Defines the 22 state features and six actions. |
| `allocation/rl/qlearn.py` | Implements replay, TD updates, target values, and fitting. |
| `allocation/rl/policy.py` | Loads the weights and produces auction decisions. |
| `allocation/sim/dataset.py` | Builds transitions and shift episodes. |
| `scripts/build_dataset.py` | Generates the training dataset. |
| `scripts/train_q.py` | Trains and saves a Q-learning model. |
| `scripts/resolve_comparison.py` | Compares the model with the baseline using paired simulations. |
| `allocation/config/reward.yaml` | Defines the reward values and discount factor. |
