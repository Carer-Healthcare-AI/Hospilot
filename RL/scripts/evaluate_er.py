"""Evaluate the trained ER policy: paired comparison, falsification sweep, shadow check.

Run:  python scripts/evaluate_er.py

Three questions, in the order that decides whether any of this is worth reporting.

1. **Does it beat the heuristic on held-out seeds?** Paired, four metrics. Held-out because CEM
   selects on fitness, so the training seeds are exactly where the policy is most over-fitted.
2. **Did it learn the fabrication?** Perturb each outcome constant and watch the action mix. A
   policy that tracks numbers nobody measured has an advantage that exists only here.
3. **Is it safe to shadow?** The learned policy must change no allocation while shadowing, and
   the divergence rate says whether a shadow log could support a later argument at all.

A pass on all three still does not license a live deployment. RL_READINESS §2.B: a simulator
comparison answers which policy paces better, never which policy saves more patients.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.evaluate import compare, sweep_fabrication
from allocation.rl.pilot import DivergenceMonitor, SafetyGate, ShadowPolicy
from allocation.rl.policy import LinearQPolicy, QWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

WEIGHTS = Path(__file__).resolve().parents[1] / "artifacts" / "er_policy.json"


def main() -> int:
    # Parameterised because CEM is no longer the only thing that produces a policy. A TD run
    # writes `er_q_policy.online.json`, and the *only* honest comparison between the two is this
    # one: same held-out seeds, same paired shifts, same four metrics. The return printed by
    # `train_q()` is not that — it is the epsilon-greedy explorer's return on the seeds it
    # trained against, which is neither greedy nor held out.
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", default=str(WEIGHTS))
    args = parser.parse_args()
    weights_path = Path(args.weights)

    if not weights_path.exists():
        print(f"no trained policy at {weights_path}. Run scripts/train_er.py first.")
        return 1
    print(f"policy              {weights_path.name}\n")

    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    weights = QWeights.load(weights_path)

    if weights.fabrication_version and weights.fabrication_version != fab.version:
        print(
            f"WARNING: policy was fitted in world {weights.fabrication_version}, evaluating in "
            f"{fab.version}. A policy is only valid for the fabrication it trained against."
        )

    print("=" * 78)
    print("1 · PAIRED COMPARISON — held-out seeds")
    print("=" * 78)
    # Twenty-four held-out seeds, not three. The paired per-shift spread in this simulator is
    # ~430 points against effects of a few tens, so three seeds (17 shifts) gave a standard
    # error of 105 and a t of 0.39 — a run that could not have detected its own headline. The
    # report now prints t and refuses to call an unresolved difference a result.
    comparison = compare(
        config, weights, agent=AgentKind.ER, seeds=tuple(range(101, 125)), shifts=6, fab=fab
    )
    print(comparison.report())

    print()
    print("=" * 78)
    print("2 · FALSIFICATION SWEEP — did it learn the invented constants?")
    print("=" * 78)
    sweep = sweep_fabrication(
        config, weights, agent=AgentKind.ER, seeds=(201, 202), shifts=4, fab=fab
    )
    print(sweep.report())

    print()
    print("=" * 78)
    print("3 · SHADOW SAFETY — does shadowing change any allocation?")
    print("=" * 78)
    shadow = ShadowPolicy(
        HeuristicPolicy(config),
        LinearQPolicy(config, weights),
        gate=SafetyGate(config),
        monitor=DivergenceMonitor(),
    )
    baseline = generate(config, seed=301, shifts=4, fab=fab)
    shadowed = generate(config, seed=301, shifts=4, policy=shadow, fab=fab)

    identical = [t.bid for t in baseline.transitions] == [t.bid for t in shadowed.transitions]
    print(f"allocations identical to baseline   {'YES' if identical else 'NO — BUG'}")
    print(f"gate refusals                       {len(shadow.blocked)}")
    print()
    print(shadow.monitor.report())

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    resolved = comparison.resolved
    print(
        f"  return vs heuristic     {comparison.return_delta:+.1%}"
        f"   (t={comparison.t_ratio:.2f}, "
        f"{'RESOLVED' if resolved else 'NOT RESOLVED — inside the noise'})\n"
        f"  abandonments            {comparison.learned.abandonments} learned vs "
        f"{comparison.baseline.abandonments} heuristic\n"
        f"  fabrication sweep       "
        f"{'VACUOUS — proves nothing, see above' if sweep.vacuous else ('PASS' if sweep.passed else 'FAIL')}\n"
        f"  shadow safe             {'YES' if identical else 'NO'}\n"
        f"  divergence breaker      "
        f"{'TRIPPED' if shadow.monitor.tripped else 'ok'}"
    )
    if not resolved:
        print(
            "\n  The return comparison is unresolved, so the sweep and shadow results describe\n"
            "  a policy whose advantage has not been demonstrated. Fix the sample before\n"
            "  reading anything into the other two."
        )
    if comparison.learned.abandonments > comparison.baseline.abandonments:
        print(
            f"\n  The learned policy abandons more patients than the heuristic "
            f"({comparison.learned.abandonments} vs {comparison.baseline.abandonments}).\n"
            "  WITHDRAW_UNPLANNED is the one action with no onward plan. Any return advantage\n"
            "  bought with it is not an improvement, it is a different rationing decision."
        )
    print(
        "\n  Whatever these say, they are statements about a simulator whose outcome model is\n"
        "  invented. RL_READINESS section 2.B: this answers which policy paces better, never\n"
        "  which policy saves more patients."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
