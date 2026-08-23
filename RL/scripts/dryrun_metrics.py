"""Dry-run the nine-metric scorecard, showing EVERY step of the arithmetic.

Run:  python scripts/dryrun_metrics.py [--seeds 10] [--weights P] [--out P]

``scripts/scorecard.py`` prints the nine numbers. It does not print how it got there, so a
reader who wants to check row 1 has to read the source, find ``measure``, find ``Comparison``,
and reconstruct the chain by hand. This script does that reconstruction on paper: raw episode
returns, the mean, the paired subtraction, the standard error, the t-ratio, each shown with its
inputs.

DEFAULT IS 10 SEEDS, NOT 100. The point is a trace you can follow with a calculator, and 689
paired shifts is not that. The full-scale numbers from artifacts/scorecard.*.log are printed
beside each row for cross-check, so you can see the small sample land in the same place -- or
not, which is itself worth knowing given a 10-seed slice carries real noise.

Nothing here re-implements a metric. Every number comes from the same ``compare()`` /
``measure()`` the real scorecard calls; this script only opens up the intermediate values that
the scorecard discards.
"""

from __future__ import annotations

import argparse
import math
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
OUT = ROOT / "artifacts"
RULE = "=" * 96
THIN = "-" * 96

#: Full-scale results, read off the committed scorecard logs, for side-by-side cross-check.
FULL_SCALE = {
    "er_policy.D_672ev_pop48.json": {
        "log": "scorecard.D_672ev_pop48.log", "label": "CEM cell D",
        "r1_ref": 713.93, "r1_pol": 782.25, "r1_delta": "+9.6%", "r1_t": 5.08, "r1_n": 689,
        "r2": "59.2%", "r3": "83.7%", "r3b": "7.5%", "r4": -68.32, "r5": 360.75,
        "r6": 0, "r7": 409.26, "r8": "40.8%",
    },
    "er_q_policy.json": {
        "log": "scorecard.Qoffline.log", "label": "Q-learning, offline",
        "r1_ref": 713.93, "r1_pol": 217.07, "r1_delta": "-69.6%", "r1_t": -26.74, "r1_n": 689,
        "r2": "65.0%", "r3": "50.1%", "r3b": "5.1%", "r4": 496.86, "r5": 1143.68,
        "r6": 17, "r7": 412.59, "r8": "35.0%",
    },
    "er_q_policy.online.json": {
        "log": "scorecard.Qonline.log", "label": "Q-learning, online",
        "r1_ref": 713.93, "r1_pol": 648.84, "r1_delta": "-9.1%", "r1_t": -4.55, "r1_n": 689,
        "r2": "26.0%", "r3": "75.7%", "r3b": "10.3%", "r4": 65.08, "r5": 605.46,
        "r6": 358, "r7": 365.06, "r8": "74.0%",
    },
}


def percentile(values, q):
    """Identical to scorecard.py:58-73. Repeated verbatim so the trace shows the real rule."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dryrun(weights_path: Path, n_seeds: int, seed_start: int, shifts: int,
           shadow_seeds: int, out_path: Path) -> None:
    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    weights = QWeights.load(weights_path)
    seeds = tuple(range(seed_start, seed_start + n_seeds))
    ref = FULL_SCALE.get(weights_path.name, {})

    lines: list[str] = []
    say = lines.append

    say(RULE)
    say(f"DRY RUN - nine-metric scorecard, every step shown")
    say(RULE)
    say(f"started              {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"policy               {weights_path.name}   ({ref.get('label', 'unknown arm')})")
    say(f"encoder / fabrication {weights.encoder_version} / {weights.fabrication_version}")
    say(f"seeds                {n_seeds} ({seeds[0]}-{seeds[-1]}) x {shifts} shifts")
    say(f"shadow seeds         {shadow_seeds} (rows 2, 6-sub and 8 only)")
    if ref:
        say(f"cross-check against  artifacts/{ref['log']}  (100 seeds, 689 shifts)")
    say("")
    say("  This is a SUBSET. A 10-seed slice is for following the arithmetic, not for")
    say("  deciding anything. Where the subset and the full run disagree, the full run is")
    say("  the number of record.")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("STEP 0 - RUN BOTH POLICIES THROUGH THE SAME WORLDS")
    say(THIN)
    say("  evaluate.compare() calls measure() twice on the SAME seeds:")
    say("    baseline = measure(..., policy=None)              -> the heuristic acts")
    say("    learned  = measure(..., policy=MixedPolicy(...))  -> the fitted policy acts")
    say("  Same worlds, same shifts, same patients. Only the decisions differ.")
    say("")
    comparison = compare(config, weights, agent=AgentKind.ER, seeds=seeds, shifts=shifts, fab=fab)
    learned, base = comparison.learned, comparison.baseline
    say(f"  baseline episodes scored   {len(base.returns_by_shift)}")
    say(f"  learned  episodes scored   {len(learned.returns_by_shift)}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROW 1 - AVERAGE EPISODE REWARD")
    say(THIN)
    say("  formula   fmean(e.discounted_return for e in complete_episodes if e.agent is ER)")
    say("  code      evaluate.py:255  (measure)")
    say("")
    own = list(learned.returns_by_shift.values())
    refr = list(base.returns_by_shift.values())
    say("  the raw per-shift returns that go into the mean (first 8 of "
        f"{len(own)}), keyed (seed, shift_id):")
    say(f"    {'key':<34} {'HEURISTIC':>12} {'POLICY':>12}")
    for k in sorted(learned.returns_by_shift)[:8]:
        say(f"    {str(k):<34} {base.returns_by_shift[k]:>12.2f} "
            f"{learned.returns_by_shift[k]:>12.2f}")
    say("")
    say(f"  heuristic  sum {sum(refr):>12.2f} / n {len(refr):<5} = {statistics.fmean(refr):>10.2f}")
    say(f"  policy     sum {sum(own):>12.2f} / n {len(own):<5} = {statistics.fmean(own):>10.2f}")
    say("")
    say("  delta = (policy - heuristic) / abs(heuristic)")
    say(f"        = ({learned.discounted_return:.2f} - {base.discounted_return:.2f}) / "
        f"{abs(base.discounted_return):.2f}")
    say(f"        = {comparison.return_delta:+.4f}  =  {comparison.return_delta:+.1%}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("  THE t-RATIO - why the delta alone is not the finding")
    say(THIN)
    diffs = list(comparison.paired_diffs)
    say("  paired_diffs subtracts SHIFT BY SHIFT on the shifts both policies completed,")
    say("  NOT mean minus mean. evaluate.py:93-100.")
    say("")
    say(f"    {'key':<34} {'policy':>10} {'-':>3} {'heuristic':>10} {'=':>3} {'diff':>10}")
    for k in sorted(set(base.returns_by_shift) & set(learned.returns_by_shift))[:8]:
        d = learned.returns_by_shift[k] - base.returns_by_shift[k]
        say(f"    {str(k):<34} {learned.returns_by_shift[k]:>10.2f} {'-':>3} "
            f"{base.returns_by_shift[k]:>10.2f} {'=':>3} {d:>+10.2f}")
    say(f"    ... {len(diffs)} paired shifts in total")
    say("")
    mean_d = statistics.fmean(diffs)
    sd_d = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
    se = sd_d / math.sqrt(len(diffs)) if len(diffs) > 1 else float("inf")
    say(f"  mean(diffs)  = {sum(diffs):>12.2f} / {len(diffs):<5} = {mean_d:>10.4f}")
    say(f"  stdev(diffs) = {sd_d:>10.4f}")
    say(f"  SE           = stdev / sqrt(n) = {sd_d:.4f} / sqrt({len(diffs)}) "
        f"= {sd_d:.4f} / {math.sqrt(len(diffs)):.4f} = {se:.4f}")
    say(f"  t            = mean / SE = {mean_d:.4f} / {se:.4f} = {comparison.t_ratio:+.4f}")
    say("")
    say(f"  |t| >= 2.0 required to call anything resolved  ->  "
        f"{'RESOLVED' if comparison.resolved else 'NOT RESOLVED'}")
    say(f"  shifts_needed = ceil((2*sd/mean)^2) = {comparison.shifts_needed}")
    say("")
    say("  WHY PAIRED. The per-shift spread is enormous - look at the raw column above. An")
    say("  unpaired comparison divides by that spread and buries any real effect. Pairing")
    say("  cancels the world-to-world luck because both policies faced the same world.")
    if ref:
        say("")
        say(f"  FULL RUN (689 shifts): reference {ref['r1_ref']}  policy {ref['r1_pol']}  "
            f"{ref['r1_delta']}, t={ref['r1_t']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROWS 2 and 8 - REFERENCE AGREEMENT and POLICY CHANGE RATE")
    say(THIN)
    say("  A SEPARATE RUN from rows 1/3/4/5/6/7. ShadowPolicy lets the HEURISTIC act and")
    say("  merely records what the learned policy would have done (pilot.py:271-282). No")
    say("  learned decision reaches an auction.")
    say(f"  It runs on the first {shadow_seeds} seeds only, not all {n_seeds}.")
    say("")
    shadow = ShadowPolicy(
        HeuristicPolicy(config), LinearQPolicy(config, weights),
        gate=SafetyGate(config), monitor=DivergenceMonitor(),
    )
    for s in seeds[:shadow_seeds]:
        generate(config, seed=s, shifts=shifts, policy=shadow, fab=fab)
    obs = shadow.monitor.observed
    rate = shadow.monitor.rate
    say(f"  decisions observed   {obs}")
    say(f"  disagreements        {round(rate * obs)}")
    say(f"  rate  = disagreements / observed = {round(rate * obs)} / {obs} = {rate:.4f}")
    say("")
    say(f"  ROW 8  Policy Change Rate  = rate       = {rate:>7.1%}")
    say(f"  ROW 2  Reference Agreement = 1 - rate   = {1 - rate:>7.1%}")
    say(f"         check: {rate:.1%} + {1 - rate:.1%} = {rate + (1 - rate):.1%}")
    say("")
    say("  *** ROWS 2 AND 8 ARE ONE MEASUREMENT PRINTED TWICE. The scorecard has nine rows")
    say("  and eight measurements. ***")
    say("")
    say("  And row 2 RANKS BACKWARDS. Agreement is similarity to the heuristic, and the")
    say("  heuristic is not an oracle. The worst policy in this project (offline Q, -69.6%)")
    say("  scores the HIGHEST agreement at 65.0%. Agreement cannot proxy correctness.")
    say("")
    say("  most common disagreements:")
    for k, v in list(shadow.monitor.disagreements.items())[:6]:
        say(f"    {k:<50} {v:>5}")
    if ref:
        say("")
        say(f"  FULL RUN: agreement {ref['r2']}   change rate {ref['r8']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROW 3 - ALLOCATION EFFICIENCY, and the beds-unallocated sub-row")
    say(THIN)
    say("  formula   ranked_ok / ranked")
    say("  where     for each auction with a winner:")
    say("              ranked    += 1")
    say("              ranked_ok += 1 if won[0].agent is max(group, key=utility).agent")
    say("  code      evaluate.py:238-247")
    say("")
    say("  In words: of the beds that WERE allocated, how often did the bidder with the")
    say("  highest clinical utility get it.")
    say("")
    say(f"    heuristic   {base.ranking_respect:>7.1%}")
    say(f"    policy      {learned.ranking_respect:>7.1%}")
    say("")
    say("  SUB-ROW  beds unallocated = 1 - awarded/auctions       (evaluate.py:259)")
    say(f"    heuristic   {base.unallocated:>7.1%}")
    say(f"    policy      {learned.unallocated:>7.1%}")
    say("")
    say("  DIFFERENT DENOMINATORS. Efficiency is over AWARDED auctions; unallocated is over")
    say("  ALL auctions. A policy can raise efficiency by refusing hard auctions, which shows")
    say("  up only in the sub-row. They are complementary and NOT substitutes - which is why")
    say("  PPO's peak fails the gate: good return, good efficiency, 11% unallocated.")
    if ref:
        say("")
        say(f"  FULL RUN: efficiency {ref['r3']}   unallocated {ref['r3b']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROWS 4 and 5 - AVERAGE REGRET and P90 REGRET")
    say(THIN)
    say("  regret = reference - policy = the paired diff with the sign flipped")
    say("  code     scorecard.py:134")
    say("")
    regrets = [-d for d in diffs]
    say(f"  first 8 regrets:  {[round(r, 2) for r in regrets[:8]]}")
    say(f"  ROW 4  mean = {sum(regrets):>12.2f} / {len(regrets):<5} = "
        f"{statistics.fmean(regrets):>10.2f}")
    say("         negative means the policy BEAT the reference")
    say("")
    say("  ROW 5  percentile is linear-interpolated (scorecard.py:58-73), NOT")
    say("         statistics.quantiles - P90 of 689 points should not round to a twentieth.")
    say("           pos = q*(n-1);  lo = int(pos);  value = s[lo] + (s[hi]-s[lo])*(pos-lo)")
    say("")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        say(f"    P{int(q * 100):<3}  {percentile(regrets, q):>+10.2f}")
    say(f"    worse than reference on {sum(1 for r in regrets if r > 0)}/{len(regrets)} shifts")
    say("")
    say("  Read the SIGN COUNT against the mean. A policy can win on average while losing on")
    say("  most shifts - that is a minority of large recoveries, not broadly better decisions,")
    say("  and the two readings say different things.")
    if ref:
        say("")
        say(f"  FULL RUN: mean regret {ref['r4']}   P90 {ref['r5']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROW 6 - CRITICAL MISS (PROXY)")
    say(THIN)
    say("  formula   raw count of dataset.abandonments, summed over seeds")
    say("  code      evaluate.py:216")
    say("")
    say(f"    heuristic   {base.abandonments:>5d}")
    say(f"    policy      {learned.abandonments:>5d}")
    say(f"    SUB-ROW  gate refusals = len(shadow.blocked) = {len(shadow.blocked)}")
    say("")
    say("  IT IS A PROXY. The real definition is NEWS2 >= 7 deterioration (checklist A.6).")
    say("  Abandonment count is what the simulator can actually observe. Do not report this")
    say("  as a clinical harm rate.")
    say("")
    say("  This row is scored as its own verdict and is NEVER merged into the return number.")
    if ref:
        say(f"\n  FULL RUN: abandonments {ref['r6']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROW 7 - REWARD STABILITY")
    say(THIN)
    say("  formula   stdev(learned.returns_by_shift.values())   -- spread WITHIN one policy")
    say("  code      scorecard.py:152-155")
    say("")
    sd_own = statistics.stdev(own) if len(own) > 1 else 0.0
    sd_ref = statistics.stdev(refr) if len(refr) > 1 else 0.0
    mean_own = statistics.fmean(own) if own else 0.0
    say(f"    heuristic sd  {sd_ref:>10.2f}")
    say(f"    policy    sd  {sd_own:>10.2f}")
    say(f"    CV = sd / mean = {sd_own:.2f} / {mean_own:.2f} = "
        f"{sd_own / mean_own if mean_own else 0:.1%}")
    say(f"    SUB-ROW  paired-diff sd = {sd_d:.2f}")
    say("")
    say("  CV is the row that matters. A low mean with a large sd gives a huge CV, which is")
    say("  how offline Q reads 190.1% at full scale - the policy is not merely worse, it is")
    say("  erratic.")
    say("")
    say("  ACROSS-SEED sd is unavailable for every arm: each cell ran train(seed=0). This is a")
    say("  known n=1 limitation, not an omission.")
    if ref:
        say(f"\n  FULL RUN: policy sd {ref['r7']}")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("ROW 9 - MEAN dQ")
    say(THIN)
    say("  n/a for CEM: there is no Bellman update, so there is no dQ to average. Read the")
    say("  CEM sigma instead.")
    say("  DEFINABLE but NOT WIRED UP for Q-learning and for PPO (as mean dV). Currently")
    say("  prints n/a for every arm, so the scorecard reports eight of nine rows for CEM and")
    say("  eight of nine for Q.")
    say("")

    # ---------------------------------------------------------------------------------
    say(THIN)
    say("TWO VERDICTS, NEVER MERGED  (checklist H.2)")
    say(THIN)
    safety = "PASS" if learned.abandonments <= base.abandonments else "FAIL"
    cap = "RESOLVED" if comparison.resolved else "NOT RESOLVED"
    say(f"  SAFETY    {safety}  - {learned.abandonments} abandonments vs "
        f"{base.abandonments} reference")
    say(f"  CAPACITY  {cap}  - {comparison.return_delta:+.1%} at t={comparison.t_ratio:.2f}")
    say("")
    say("  A capacity gain does not buy off a safety regression. They are reported separately")
    say("  and a FAIL on safety is disqualifying regardless of return.")
    say("")
    say(THIN)
    say("WHAT NONE OF THESE NINE ROWS MEASURE")
    say(THIN)
    say("  Whether a BETTER ACTION EXISTED at any decision point. Every row above is distance")
    say("  from the shipped heuristic, and the heuristic is four unfitted rules, not a")
    say("  ceiling. No substitution among these metrics reaches optimality. That is checklist")
    say("  section C, gated on section 0.")
    say("")
    say(f"finished             {datetime.now():%Y-%m-%d %H:%M:%S}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {out_path.name:<40} {len(lines):>4} lines   "
          f"[{comparison.return_delta:+.1%}, t={comparison.t_ratio:.2f}]")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--seed-start", type=int, default=101)
    parser.add_argument("--shifts", type=int, default=6)
    parser.add_argument("--shadow-seeds", type=int, default=6)
    parser.add_argument("--weights", default=None,
                        help="one policy; default runs CEM D, offline Q and online Q")
    args = parser.parse_args(argv)

    if args.weights:
        targets = [Path(args.weights)]
    else:
        targets = [
            OUT / "er_policy.D_672ev_pop48.json",
            OUT / "er_q_policy.json",
            OUT / "er_q_policy.online.json",
        ]

    print(f"dry run: {args.seeds} seeds ({args.seed_start}-"
          f"{args.seed_start + args.seeds - 1}) x {args.shifts} shifts\n")
    for path in targets:
        if not path.exists():
            print(f"  SKIP {path.name} - not on disk")
            continue
        stem = path.name.replace("er_policy.", "").replace("er_", "").replace(".json", "")
        dryrun(path, args.seeds, args.seed_start, args.shifts, args.shadow_seeds,
               OUT / f"dryrun_metrics.{stem}.log")
    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
