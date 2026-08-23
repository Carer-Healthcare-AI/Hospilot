"""Why does PPO plateau ~4 % below the heuristic? Three measurements, three hypotheses.

Each one is chosen because it *discriminates* — it comes out differently depending on which
explanation is true, rather than merely being consistent with all of them.

1. **Explained variance of V(s).** If the critic explains ~no variance, GAE advantages are
   dominated by which shift a step landed in rather than which action was taken, and after
   per-batch normalisation every step in a rich shift gets a large positive advantage regardless
   of its action. That is a fixable baseline problem, and it is a direct route to memorising 32
   worlds. EV near 1 would rule it out.

2. **Behaviour breakdown vs the heuristic.** Run 1's final policy stopped competing: burn 28.2 %
   against 53.8 %, ER win share 23 % against 56 %, and 32.7 % of auctions unallocated. Whether
   the *plateau* policy does the same thing says whether the collapse is the endpoint of a
   gradual drift or a distinct late failure.

3. **Stochastic vs deterministic serving.** The probe scores the argmax + Beta mode. If sampling
   scores materially higher, the loss is in the serving rule (§0's determinism requirement), not
   in what was learned — a completely different remedy.

Run against the run-2 seed-0 checkpoints, which sit at the plateau.
"""

import io, json, random, statistics, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import StateEncoder
from allocation.rl.evaluate import measure
from allocation.rl.ppo import collect, values
from allocation.rl.ppo_policy import MixedPPOPolicy, PPOWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

ART = Path(__file__).resolve().parents[1] / "artifacts"
SEEDS = (11, 12, 13, 14, 15, 16, 17, 18)
VALID = tuple(range(205, 215))

config = _with_base(load_config(), 120.0)
fab = register({"arrival.bed_release_per_hour": 1.8, "arrival.candidate_per_hour": 3.6})
encoder = StateEncoder()

target = ART / "er_policy.ppo_run2_A_s0.final.json"
if not target.exists():
    target = ART / "er_policy.ppo_run2_A_s0.best.json"
weights = PPOWeights.load(target, encoder)
print(f"policy under test    {target.name}")
print(f"encoder/fabrication  {weights.encoder_version} / {weights.fabrication_version}")
print()

# ---------------------------------------------------------------------------------------
print("=" * 78)
print("1 - EXPLAINED VARIANCE OF THE CRITIC")
print("=" * 78)
batch = collect(config, weights, AgentKind.ER, SEEDS, 4, fab, random.Random(0),
                target_steps=1200, encoder=encoder)
predicted = values(weights, batch.states)
actual = batch.returns
residual = actual - predicted
ev = 1.0 - float(np.var(residual) / (np.var(actual) + 1e-12))
print(f"  steps                {batch.n}")
print(f"  target  mean {actual.mean():7.3f}   sd {actual.std():7.3f}   (scaled: reward / 200)")
print(f"  V(s)    mean {predicted.mean():7.3f}   sd {predicted.std():7.3f}")
print(f"  residual sd          {residual.std():.3f}")
print(f"  EXPLAINED VARIANCE   {ev:+.3f}")
print()
print("  1.0 = perfect critic. 0.0 = no better than predicting the mean.")
print("  Negative = WORSE than predicting the mean, i.e. the baseline actively adds variance")
print("  to the advantage rather than removing it.")
print()
corr = float(np.corrcoef(predicted, actual)[0, 1]) if predicted.std() > 1e-9 else float("nan")
print(f"  corr(V, return)      {corr:+.3f}")
print(f"  advantage sd         {batch.advantages.std():.3f}  (normalised, so ~1 by construction)")
print()

# ---------------------------------------------------------------------------------------
print("=" * 78)
print("2 - WHAT THE PLATEAU POLICY ACTUALLY DOES")
print("=" * 78)
base = measure(config, "heuristic", VALID, 4, fab, AgentKind.ER, None, encoder)
det = measure(config, "ppo-argmax", VALID, 4, fab, AgentKind.ER,
              MixedPPOPolicy(config, weights, AgentKind.ER, encoder, deterministic=True),
              encoder)
print(f"  {'policy':<14}{'return':>9}{'burn':>8}{'rank':>8}{'noaward':>9}{'aband':>7}   win share")
for m in (base, det):
    shares = " ".join(f"{a}:{v:.0%}" for a, v in sorted(m.win_share.items()))
    print(f"  {m.label:<14}{m.discounted_return:>9.2f}{m.burn:>8.1%}{m.ranking_respect:>8.1%}"
          f"{m.unallocated:>9.1%}{m.abandonments:>7}   {shares}")
print()
print("  action mix (learned agent's decisions)")
keys = sorted(set(base.action_mix) | set(det.action_mix))
print(f"  {'action':<24}{'heuristic':>11}{'ppo':>9}")
for k in keys:
    print(f"  {k:<24}{base.action_mix.get(k, 0):>10.1%}{det.action_mix.get(k, 0):>9.1%}")
print()

# ---------------------------------------------------------------------------------------
print("=" * 78)
print("3 - SERVING RULE: ARGMAX+MODE vs SAMPLING")
print("=" * 78)
sto = measure(config, "ppo-sampled", VALID, 4, fab, AgentKind.ER,
              MixedPPOPolicy(config, weights, AgentKind.ER, encoder, deterministic=False,
                             rng=random.Random(7)),
              encoder)
print(f"  heuristic            {base.discounted_return:8.2f}")
print(f"  ppo deterministic    {det.discounted_return:8.2f}   "
      f"({(det.discounted_return - base.discounted_return) / base.discounted_return:+.1%})")
print(f"  ppo sampled          {sto.discounted_return:8.2f}   "
      f"({(sto.discounted_return - base.discounted_return) / base.discounted_return:+.1%})")
print()
gap = sto.discounted_return - det.discounted_return
print(f"  sampled - deterministic  {gap:+.2f}")
print("  A large positive gap would mean the loss is in the SERVING rule, not in what was")
print("  learned. A small or negative gap means deterministic serving is not the problem.")
