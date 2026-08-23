"""Is the collapse in what was LEARNED, or in how it is SERVED?

The ablation left a puzzle. Arm B draws fresh worlds every rollout, so its training return is
already an out-of-sample measurement — and it still rose while the validation probe fell. Two
things differ between those two numbers, not one:

* the worlds (11-18 or a fresh draw, versus 205-300), and
* the serving rule — the training return is the policy **sampling** its actions and its alpha,
  the probe is argmax + the Beta mode.

The ablation controlled the first and found nothing. This controls the second: score each saved
checkpoint on the SAME validation worlds under both rules. If sampled tracks the training curve
upward while deterministic falls, the loss is in the serving rule and no amount of entropy or
learning-rate tuning addresses it. If both fall together, the policy genuinely got worse and the
update dynamics are the place to look.

Cheap by construction: it re-scores checkpoints that already exist rather than training anything.
"""

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import StateEncoder
from allocation.rl.evaluate import measure
from allocation.rl.ppo_policy import MixedPPOPolicy, PPOWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

ART = Path(__file__).resolve().parents[1] / "artifacts"
#: A 32-seed slice of the clean band. Narrower than the 96 the training probe uses, because this
#: asks for a DIFFERENCE between two rules on identical worlds, which is paired and far less
#: noisy than either level on its own.
VALID = tuple(range(205, 237))

config = _with_base(load_config(), 120.0)
fab = register({"arrival.bed_release_per_hour": 1.8, "arrival.candidate_per_hour": 3.6})
encoder = StateEncoder()

label = sys.argv[1] if len(sys.argv) > 1 else "abl_A_fixed_s0"
checkpoints = sorted(ART.glob(f"er_policy.ppo_{label}.it*.json"))
if not checkpoints:
    print(f"no checkpoints for {label}")
    raise SystemExit(1)

base = measure(config, "heuristic", VALID, 4, fab, AgentKind.ER, None, encoder)
print(f"checkpoints from   {label}")
print(f"validation worlds  seeds {VALID[0]}-{VALID[-1]} x 4 shifts")
print(f"heuristic          {base.discounted_return:.2f}   no-award {base.unallocated:.1%}")
print()
print(f"  {'ckpt':>7}{'determ':>9}{'sampled':>9}{'sampled-det':>13}"
      f"{'det noawd':>11}{'smp noawd':>11}{'det wdalt':>11}{'smp wdalt':>11}")

for path in checkpoints:
    weights = PPOWeights.load(path, encoder)
    det = measure(config, "det", VALID, 4, fab, AgentKind.ER,
                  MixedPPOPolicy(config, weights, AgentKind.ER, encoder, deterministic=True),
                  encoder)
    # Averaged over three sampling streams: one draw of a stochastic policy on 32 worlds is a
    # noisy read, and the quantity of interest is a difference against the deterministic score.
    runs = [
        measure(config, "smp", VALID, 4, fab, AgentKind.ER,
                MixedPPOPolicy(config, weights, AgentKind.ER, encoder, deterministic=False,
                               rng=random.Random(7 + k)),
                encoder)
        for k in range(3)
    ]
    smp = statistics.fmean(r.discounted_return for r in runs)
    smp_no = statistics.fmean(r.unallocated for r in runs)
    smp_wd = statistics.fmean(r.action_mix.get("withdraw_alternative", 0.0) for r in runs)
    it = path.stem.split(".it")[-1]
    print(f"  {it:>7}{det.discounted_return:>9.1f}{smp:>9.1f}"
          f"{smp - det.discounted_return:>+13.1f}"
          f"{det.unallocated:>11.1%}{smp_no:>11.1%}"
          f"{det.action_mix.get('withdraw_alternative', 0.0):>11.1%}{smp_wd:>11.1%}")

print()
print("  A large and GROWING positive `sampled-det` means the serving rule is the loss: the")
print("  policy still knows how to play, and argmax+mode is what throws it away. A flat or")
print("  negative gap means the policy itself degraded, and the update dynamics are the target.")
