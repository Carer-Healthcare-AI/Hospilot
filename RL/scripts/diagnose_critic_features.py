"""Is the critic starved of features, or starved of fit?

Two different failures produce a low explained variance and they need opposite remedies:

* **Feature starvation.** No linear function of the 22 features can predict the remaining
  shift return. Then OLS on those features hits a low R-squared too, and the only fix is a
  new feature.
* **Fit starvation.** A linear function *can* predict it, but the trained ``v_row`` has not
  reached it. Then OLS scores far above the live critic, and adding features fixes nothing.

So: measure the trained critic, then measure the best possible linear critic over the same
vector, then measure what an ORACLE remaining-auction count would add on top. The oracle is
never a candidate feature -- it is the ceiling that says how much room a legitimate proxy
could possibly buy.

Nothing here trains anything or writes a checkpoint.
"""

import datetime as dt
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import ACTION_INDEX, NAMES, StateEncoder
from allocation.rl.ppo import values
from allocation.rl.ppo_policy import MixedPPOPolicy, PPOWeights
from allocation.reward.terms import discount_gamma, maximum_reward
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

ART = Path(__file__).resolve().parents[1] / "artifacts"
SEEDS = (11, 12, 13, 14, 15, 16, 17, 18)

config = _with_base(load_config(), 120.0)
fab = register({"arrival.bed_release_per_hour": 1.8, "arrival.candidate_per_hour": 3.6})
encoder = StateEncoder()
GAMMA = discount_gamma(config)
SCALE = max(1.0, maximum_reward(config))
RELEASE_RATE = fab["arrival.bed_release_per_hour"]

target = ART / "er_policy.ppo_run2_A_s0.final.json"
weights = PPOWeights.load(target, encoder)
print(f"policy            {target.name}")
print(f"gamma {GAMMA}   reward scale {SCALE}   release rate {RELEASE_RATE}/h")
print()


def collect_with_metadata(agent, seeds, shifts, target_steps, lam=0.95, seed=0):
    """``ppo.collect``, plus the per-step bookkeeping it throws away.

    Mirrors it on the parts that matter -- same join, same GAE, same terminal rule -- so the
    returns here are the returns the trainer actually regresses onto.
    """
    from allocation.rl.ppo import _final_per_auction

    rng = random.Random(seed)
    states, returns, meta = [], [], []
    cursor = 0
    while len(states) < target_steps:
        world_seed = seeds[cursor % len(seeds)]
        cursor += 1
        policy = MixedPPOPolicy(
            config, weights, agent, encoder,
            rng=random.Random(rng.randrange(2**31)),
            deterministic=False, mask_unplanned=True,
        )
        dataset = generate(config, seed=world_seed, shifts=shifts, policy=policy, fab=fab,
                           encoder=encoder)
        logged = _final_per_auction(policy.log, agent)
        usable = {(e.agent, e.shift_id) for e in dataset.complete_episodes}

        chains = defaultdict(list)
        for transition in dataset.transitions:
            if transition.agent is not agent:
                continue
            if (transition.agent, transition.shift_id) not in usable:
                continue
            chains[transition.shift_id].append(transition)

        for shift_id, chain in chains.items():
            rows = []
            for transition in chain:
                entry = logged.get(transition.auction_id)
                if entry is None or entry.action_index != ACTION_INDEX[transition.q_action]:
                    continue
                rows.append((entry, transition))
            if not rows:
                continue

            vals = [entry.value for entry, _ in rows]
            gae = 0.0
            adv = [0.0] * len(rows)
            for t in reversed(range(len(rows))):
                nxt = vals[t + 1] if t + 1 < len(rows) else 0.0
                delta = rows[t][1].reward / SCALE + GAMMA * nxt - vals[t]
                gae = delta + GAMMA * lam * gae
                adv[t] = gae

            # The pure Monte-Carlo return, for reference: what GAE(lam=1) would target.
            mc = [0.0] * len(rows)
            acc = 0.0
            for t in reversed(range(len(rows))):
                acc = rows[t][1].reward / SCALE + GAMMA * acc
                mc[t] = acc

            for t, (entry, transition) in enumerate(rows):
                states.append(entry.state)
                returns.append(adv[t] + vals[t])
                meta.append({
                    "seed": world_seed, "shift_id": shift_id,
                    "t": t, "chain_len": len(rows), "remaining": len(rows) - t,
                    "mc_return": mc[t],
                    "reward": transition.reward / SCALE,
                    "action": transition.q_action.value,
                })
    return np.asarray(states, dtype=float), np.asarray(returns, dtype=float), meta


CACHE = ART / "critic_rollout.npz"
if CACHE.exists():
    blob = np.load(CACHE, allow_pickle=True)
    X, y, meta = blob["X"], blob["y"], list(blob["meta"])
    print(f"rollout           reused from {CACHE.name}")
else:
    X, y, meta = collect_with_metadata(AgentKind.ER, SEEDS, 4, target_steps=2400)
    np.savez(CACHE, X=X, y=y, meta=np.array(meta, dtype=object))
    print(f"rollout           collected and cached to {CACHE.name}")
n = len(y)
elapsed = X[:, NAMES.index("shift_fraction_elapsed")]
remaining = np.array([m["remaining"] for m in meta], dtype=float)
chain_len = np.array([m["chain_len"] for m in meta], dtype=float)
mc = np.array([m["mc_return"] for m in meta], dtype=float)

print("=" * 82)
print("0 - THE SAMPLE")
print("=" * 82)
print(f"  steps {n}   chains {len(set((m['seed'], m['shift_id']) for m in meta))}")
print(f"  chain length      mean {chain_len.mean():5.2f}  sd {chain_len.std():5.2f}  "
      f"min {chain_len.min():.0f}  max {chain_len.max():.0f}")
print(f"  GAE return        mean {y.mean():7.3f}  sd {y.std():7.3f}")
print(f"  MC  return        mean {mc.mean():7.3f}  sd {mc.std():7.3f}")
print(f"  per-step reward   mean {np.mean([m['reward'] for m in meta]):7.3f}")
print(f"  elapsed           mean {elapsed.mean():7.3f}  sd {elapsed.std():7.3f}  "
      f"min {elapsed.min():.3f}  max {elapsed.max():.3f}")
print()


def ev(pred, actual):
    return 1.0 - float(np.var(actual - pred) / (np.var(actual) + 1e-12))


def ols_r2(cols, actual):
    """R-squared of the best linear fit. In-sample: a CEILING, not a generalisation claim."""
    A = np.column_stack([np.ones(len(actual))] + list(cols))
    beta, *_ = np.linalg.lstsq(A, actual, rcond=None)
    return ev(A @ beta, actual), beta


ALL = [X[:, i] for i in range(X.shape[1])]

print("=" * 82)
print("1 - THE TRAINED CRITIC AS IT STANDS")
print("=" * 82)
V = values(weights, X)
print(f"  sd(return)           {y.std():.3f}")
print(f"  sd(V)                {V.std():.3f}")
print(f"  corr(V, return)      {float(np.corrcoef(V, y)[0, 1]):+.3f}")
print(f"  EXPLAINED VARIANCE   {ev(V, y):+.3f}")
rescaled, _ = ols_r2([V], y)
print(f"  EV after refitting only a slope+intercept on V:  {rescaled:+.3f}")
print("    -> the gap between these two is pure MIS-SCALING: the critic points the right way")
print("       and is too flat. No new feature is needed to recover that part.")
print()

print("=" * 82)
print("2 - CEILING OF THE CURRENT 22 FEATURES (best possible linear critic)")
print("=" * 82)
r2_all, _ = ols_r2(ALL, y)
r2_elapsed, _ = ols_r2([elapsed], y)
print(f"  OLS R^2 on all 22 features                  {r2_all:+.3f}")
print(f"  OLS R^2 on shift_fraction_elapsed alone     {r2_elapsed:+.3f}")
print()
print("  single-feature R^2, ranked:")
singles = sorted(((ols_r2([X[:, i]], y)[0], NAMES[i]) for i in range(X.shape[1])), reverse=True)
for score, name in singles[:8]:
    print(f"    {name:<26}{score:+.3f}")
print()

print("=" * 82)
print("3 - THE ORACLE CEILING (not a candidate feature -- an upper bound)")
print("=" * 82)
disc = (1.0 - GAMMA ** remaining) / (1.0 - GAMMA)
r2_oracle, _ = ols_r2([remaining], y)
r2_disc, _ = ols_r2([disc], y)
r2_all_oracle, _ = ols_r2(ALL + [remaining, disc], y)
print(f"  OLS R^2 on TRUE remaining-auction count alone      {r2_oracle:+.3f}")
print(f"  OLS R^2 on (1-g^k)/(1-g), the exact horizon term   {r2_disc:+.3f}")
print(f"  OLS R^2 on 22 features + oracle count              {r2_all_oracle:+.3f}")
print(f"  headroom the oracle buys over the 22 features      {r2_all_oracle - r2_all:+.3f}")
print()
print(f"  corr(shift_fraction_elapsed, true remaining count) "
      f"{float(np.corrcoef(elapsed, remaining)[0, 1]):+.3f}")
print("    -> if this is strongly negative, elapsed time ALREADY carries the horizon and a new")
print("       'expected auctions remaining' feature is a rescaling of a column we already have.")
print()

print("=" * 82)
print("4 - CANDIDATE LEGITIMATE FEATURES (all computable at decision time)")
print("=" * 82)
shift_hours = 8.0
hours_left = (1.0 - elapsed) * shift_hours
boarding = X[:, NAMES.index("boarding")]
budget = X[:, NAMES.index("budget_remaining")]
cands = {
    "hours_left (= 1-elapsed, rescaled)": hours_left,
    "expected_releases_remaining = rate*hours_left": RELEASE_RATE * hours_left,
    "expected_auctions_remaining, discounted": (1 - GAMMA ** (RELEASE_RATE * hours_left)) / (1 - GAMMA),
    "expected * boarding": RELEASE_RATE * hours_left * boarding,
    "budget_remaining * hours_left": budget * hours_left,
    "elapsed^2": elapsed ** 2,
}
print(f"  {'candidate':<48}{'alone':>9}{'+22 feats':>11}{'delta':>9}")
for label, col in cands.items():
    alone, _ = ols_r2([col], y)
    joint, _ = ols_r2(ALL + [col], y)
    print(f"  {label:<48}{alone:>+9.3f}{joint:>+11.3f}{joint - r2_all:>+9.3f}")
print()

print("=" * 82)
print("5 - IS THE SIM'S DEMAND/SUPPLY SIGNAL EVEN VARYING?")
print("=" * 82)
from allocation.sim.world import SimWorld

w = SimWorld(seed=11, fab=fab)
disc_vals, dem_vals, board_vals = set(), set(), set()
for h in range(1, 33):
    moment = w.start + dt.timedelta(hours=h)
    w.arrivals_until(moment)
    w.advance_to(moment)
    s = w.state("icu")
    disc_vals.add(s.expected_discharges_4h)
    dem_vals.add(s.predicted_demand_4h)
    board_vals.add(s.boarding_count)
print(f"  expected_discharges_4h distinct  {sorted(disc_vals)}")
print(f"  predicted_demand_4h    distinct  {sorted(dem_vals)[:12]}")
print(f"  boarding_count         distinct  {sorted(board_vals)[:12]}")
