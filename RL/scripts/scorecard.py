"""The nine-metric scorecard, with the HEURISTIC as reference instead of an oracle.

Run:  python scripts/scorecard.py --weights artifacts/er_policy.D_672ev_pop48.json
                                 --out artifacts/scorecard.D.log

``RL_EVAL_CHECKLIST.md`` §G0 binds ``RL_METRIC.md``'s nine rows to code and finds three of them
undefined without the oracle of §C. This script is the decision to substitute the heuristic for
that oracle and report all nine anyway. **The substitution changes what three rows mean, and the
output says so in the banner it prints.**

What the substitution costs, stated once:

* The reference is four rules transcribed from a worked example, never fitted
  (``policy/heuristic.py:1-21``). It is not a ceiling, so "regret" here can be **negative** — a
  policy better than the reference produces negative regret, which under ``RL_METRIC.md``'s
  reading ("reward lost vs optimal") is not a meaningful quantity. Read rows 4 and 5 as *"return
  forgone against the shipped baseline"* and nothing more.
* Reference agreement is NOT an optimal-action rate. A decision where the policy is right and the
  heuristic is wrong is scored here as a disagreement, i.e. as a miss. The metric cannot tell the
  two apart, which is the whole reason §C exists.
* Nothing here measures whether a better action existed. No substitution reaches that.

Rows 1, 3, 6, 7 and 8 are unaffected by the substitution and are real measurements. That is why
this script exists rather than the request being refused.
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.evaluate import compare
from allocation.rl.pilot import DivergenceMonitor, SafetyGate, ShadowPolicy
from allocation.rl.policy import LinearQPolicy, QWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]

#: Same guard as ``resolve_comparison.py``. A scorecard scored on a different sample than the
#: cells it is read beside is not comparable to them.
RESERVED = {"CEM fitness": range(11, 19), "fabrication sweep": range(201, 205)}

#: Control from ``artifacts/sweep_refit.log:24`` — action-mix distance between two CEM fits of
#: the SAME world under different CEM seeds. Row 8's noise floor: a policy change rate below this
#: is indistinguishable from the optimiser having landed somewhere else.
CEM_SEED_NOISE_FLOOR = 0.121


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile.

    ``statistics.quantiles`` cuts at fixed n, and P90 of a 689-point sample should not be
    rounded to the nearest twentieth when the tail is the thing being reported.
    """
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main(argv: list[str]) -> int:
    args = list(argv)
    weights_path = ROOT / "artifacts" / "er_policy.D_672ev_pop48.json"
    out_path = ROOT / "artifacts" / "scorecard.log"
    n_seeds, seed_start, shadow_seeds = 100, 101, 6
    for flag in ("--weights", "--out", "--seeds", "--seed-start", "--shadow-seeds"):
        if flag in args:
            i = args.index(flag)
            raw = args[i + 1]
            if flag == "--weights":
                weights_path = Path(raw)
            elif flag == "--out":
                out_path = Path(raw)
            elif flag == "--seeds":
                n_seeds = int(raw)
            elif flag == "--seed-start":
                seed_start = int(raw)
            else:
                shadow_seeds = int(raw)
            del args[i:i + 2]

    seeds = tuple(range(seed_start, seed_start + n_seeds))
    for purpose, reserved in RESERVED.items():
        clash = sorted(set(seeds) & set(reserved))
        if clash:
            print(f"REFUSING: seeds {clash[0]}-{clash[-1]} are reserved for {purpose}.")
            return 2

    log = out_path.open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()
        print(text, flush=True)

    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    weights = QWeights.load(weights_path)

    say("=" * 78)
    say("NINE-METRIC SCORECARD - reference: HEURISTIC, not an oracle")
    say("=" * 78)
    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"policy               {weights_path.name}")
    say(f"encoder / fabrication {weights.encoder_version} / {weights.fabrication_version}")
    say(f"seeds                {n_seeds} ({seeds[0]}-{seeds[-1]}), 6 shifts each")
    say("")
    say("  REFERENCE = allocation/policy/heuristic.py - four unfitted rules, NOT a ceiling.")
    say("  Rows 2, 4 and 5 therefore measure distance from the shipped baseline, NOT distance")
    say("  from the best available action. Negative regret means the policy beat the reference;")
    say("  it does not mean 'better than optimal'. See RL_EVAL_CHECKLIST.md section G0.")
    say("")

    comparison = compare(
        config, weights, agent=AgentKind.ER, seeds=seeds, shifts=6, fab=fab,
    )
    diffs = list(comparison.paired_diffs)
    # regret = reference - policy, so it is the paired difference with the sign flipped.
    regrets = [-d for d in diffs]
    learned, base = comparison.learned, comparison.baseline

    shadow = ShadowPolicy(
        HeuristicPolicy(config),
        LinearQPolicy(config, weights),
        gate=SafetyGate(config),
        monitor=DivergenceMonitor(),
    )
    # Shadow on the SAME range as the comparison. ``evaluate_er.py:99-100`` uses seed 301, which
    # sits inside the confirmation range (RL_EVAL_CHECKLIST §B.3 flags it as a hygiene defect);
    # this does not repeat that.
    for s in seeds[:shadow_seeds]:
        generate(config, seed=s, shifts=6, policy=shadow, fab=fab)
    agreement = 1.0 - shadow.monitor.rate

    own = list(learned.returns_by_shift.values())
    ref = list(base.returns_by_shift.values())
    sd_own = statistics.stdev(own) if len(own) > 1 else 0.0
    sd_ref = statistics.stdev(ref) if len(ref) > 1 else 0.0
    mean_own = statistics.fmean(own) if own else 0.0
    sd_diff = statistics.stdev(diffs) if len(diffs) > 1 else 0.0

    say("-" * 78)
    say(f"{'#':<3} {'metric':<24} {'reference':>11} {'policy':>11}   note")
    say("-" * 78)
    say(f"{'1':<3} {'Average Ep. Reward':<24} {base.discounted_return:>11.2f} "
        f"{learned.discounted_return:>11.2f}   {comparison.return_delta:+.1%}, "
        f"t={comparison.t_ratio:.2f}, {len(diffs)} shifts")
    say(f"{'2':<3} {'Reference Agreement':<24} {'-':>11} {agreement:>10.1%}   "
        f"{shadow.monitor.observed} shadow decisions; NOT optimal-action rate")
    say(f"{'3':<3} {'Allocation Efficiency':<24} {base.ranking_respect:>10.1%} "
        f"{learned.ranking_respect:>10.1%}   highest-utility bidder won")
    say(f"{'':<3} {'  beds unallocated':<24} {base.unallocated:>10.1%} "
        f"{learned.unallocated:>10.1%}   complementary, not a substitute")
    say(f"{'4':<3} {'Average Regret':<24} {0.0:>11.2f} "
        f"{statistics.fmean(regrets):>11.2f}   vs heuristic; negative = beat reference")
    say(f"{'5':<3} {'P90 Regret':<24} {'-':>11} {percentile(regrets, 0.90):>11.2f}   "
        f"P50 {percentile(regrets, 0.50):+.1f}  P99 {percentile(regrets, 0.99):+.1f}")
    say(f"{'6':<3} {'Critical Miss (proxy)':<24} {base.abandonments:>11d} "
        f"{learned.abandonments:>11d}   abandonment COUNT, not NEWS2>=7 per A.6")
    say(f"{'':<3} {'  gate refusals':<24} {'-':>11} {len(shadow.blocked):>11d}   "
        f"safety-layer interventions while shadowing")
    say(f"{'7':<3} {'Reward Stability sd':<24} {sd_ref:>11.2f} {sd_own:>11.2f}   "
        f"within-policy across shifts; CV {sd_own / mean_own if mean_own else 0:.1%}")
    say(f"{'':<3} {'  paired-diff sd':<24} {'-':>11} {sd_diff:>11.2f}   "
        f"across-SEED sd unavailable: every cell ran train(seed=0)")
    say(f"{'8':<3} {'Policy Change Rate':<24} {'-':>11} {shadow.monitor.rate:>10.1%}   "
        f"noise floor {CEM_SEED_NOISE_FLOOR:.1%} (sweep_refit.log:24)")
    say(f"{'9':<3} {'Mean dQ':<24} {'-':>11} {'n/a':>11}   "
        f"no Bellman update in CEM; read sigma instead")
    say("-" * 78)
    say("")
    say(f"  regret distribution over {len(regrets)} paired shifts")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        say(f"    P{int(q * 100):<3}  {percentile(regrets, q):+9.2f}")
    say(f"    worse than reference on {sum(1 for r in regrets if r > 0)}/{len(regrets)} shifts")
    say("")
    say("  most common disagreements with the reference")
    for k, v in list(shadow.monitor.disagreements.items())[:8]:
        say(f"    {k:<48} {v:>5}")
    say("")
    say("  Two verdicts, never merged (section H.2):")
    say(f"    SAFETY   {'PASS' if learned.abandonments <= base.abandonments else 'FAIL'} "
        f"- {learned.abandonments} abandonments vs {base.abandonments} reference")
    say(f"    CAPACITY {'RESOLVED' if comparison.resolved else 'NOT RESOLVED'} "
        f"- {comparison.return_delta:+.1%} at t={comparison.t_ratio:.2f}")
    say("")
    say("  NOT MEASURED HERE, and no substitution reaches it: whether a better action existed")
    say("  at any decision point. That is RL_EVAL_CHECKLIST.md section C, gated on section 0.")
    say("")
    say(f"finished             {datetime.now():%H:%M:%S}")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
