"""The labels for input.gate.*.csv: the action taken and the accumulated reward.

Run:  python scripts/export_output_csv.py [--rows 8126] [--sample head] [--mode cumulative]

Row i here is row i there. Same order, same cut, joinable on ``row_id``.

"Accumulated" is ambiguous and the two readings are not interchangeable, so both are built:

  --mode cumulative      running sum of reward WITHIN the episode, up to and including this
                         decision. Answers "what has this department banked so far this shift".
  --mode return_to_go    discounted sum FORWARD from this decision to the end of the episode,
                         gamma = 0.99. This is the RL target -- the quantity a value function
                         predicts and the thing evaluate.py aggregates into Average Ep. Reward.

The episode boundary is the SHIFT, per RL-Steps section 21, so accumulation resets on every
(seed, shift_id, agent) group and never carries across a budget reset.

CAVEAT ON --sample head: the cut lands mid-world at seed 135, so the final episodes are
truncated. Their accumulation is correct for the rows present and incomplete as episodes. The
stratified cut splits many more. Neither is a defect of this file; it is what taking 8126 of
23363 rows costs, and it is printed rather than hidden.

``synthetic_data_rewards`` -- A FAKE SECOND REWARD COLUMN, FOR DRY-RUNNING A COMPARISON.

Its only purpose is to stand in for a second model's rewards so a metric-calculation pipeline
can be run twice and the two answers diffed. It is NOT a model output, NOT a simulator roll,
and NOT derived from any policy in this repo. Nothing should ever be concluded from its VALUE
-- only from whether the comparison code that reads it behaves.

It is built to be interchangeable with ``reward`` at the point a metric consumes it:

  same support        drawn only from the 36 values ``reward`` actually takes here, so it is a
                      multiple of 5 in [-90, 170] like every real reward. A metric that bins,
                      thresholds or sums will not trip on an out-of-range value.
  same shape          resampled against ``reward``'s own empirical frequencies, so the marginal
                      distribution matches rather than merely the range.
  regime-preserving   a row that lost mostly still loses. Perturbing blindly would drop a +170
                      where a -60 was and destroy the win/lose structure every reward metric is
                      actually measuring -- which makes the two runs incomparable rather than
                      comparably different.
  correlated, not equal
                      the point is two DIFFERENT numbers that are both plausible. Identical
                      columns make every comparison read zero and prove nothing.

DETERMINISTIC. Seeded from ``--noise-seed``, so re-running produces byte-identical output and a
diff between two runs of the metric code is a real difference in the code, never a reshuffled
fake column. Change ``--noise-seed`` for a second independent draw.

Tune the disagreement with ``--agree`` (default 0.62, the share of rows left untouched).

``utility_q`` / ``utility_syn`` / ``auction_id`` -- SO ALLOCATION EFFICIENCY RUNS FROM HERE.

Row 3 of the scorecard is ``ranked_ok / awarded`` (dryrun_metrics_from_csv.py:439-449), and it
is an AUCTION-level metric, not a row-level one::

    awarded     the auction has a row with reward >= 80          (the win floor)
    ranked_ok   that winner is also the auction's highest-utility bidder

Two columns it needs were not in this file. ``utility`` lived only in input.gate.*.csv, and
``auction_id`` lived in neither -- without it the rows cannot be GROUPED into auctions at all,
so the metric was uncomputable from the output file alone however many utilities were added.
Both are here now, for the same reason agent and candidate_id already were: an output file that
cannot be read without its input is a footgun when the two drift.

  utility_q     the REAL utility, verbatim from the corpus. Raw 0-200 scale (-4.4 .. 111.9
                here), NOT the 0-1 scaled feature at state[0] -- those are the same name 200x
                apart, which is the collision export_input_csv.py's docstring warns about.
                Pairs with ``reward``.
  utility_syn   fake, same scale, same clamp. Pairs with ``synthetic_data_rewards``.

*Utility is a property of the patient, not of the method.* Two models scoring the same auction
genuinely see the same utilities, so a real comparison would use ONE utility column for both.
``utility_syn`` exists only because a dry run needs the second arm to produce a DIFFERENT
efficiency number -- if both arms shared a utility column the metric would agree with itself by
construction and the comparison would prove nothing about the code. Noise is sized to flip the
top bidder in a useful fraction of auctions, and the flip rate is printed rather than assumed.
"""
from __future__ import annotations
import argparse, bisect, csv, random, sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
SRC = OUT / "validation.gate.seeds101-200.csv"
GAMMA = 0.99
#: dryrun_metrics_from_csv.py:48. `won` is not a column here, and reward >= 80 separates the two
#: classes exactly -- four reward terms are hardcoded True on a win, so min(reward | won) is 80.
WIN_FLOOR = 80.0


def regime(r: float) -> str:
    """Where a row sits in the win/lose structure.

    Reward metrics are mostly measuring this, so the fake column has to preserve it or the two
    runs are not comparable in the first place.
    """
    return "lost" if r < 0 else ("won" if r >= 100 else "mid")


def sampler(values, rng):
    """Weighted draw from an empirical value -> count map, without numpy."""
    keys = sorted(values)
    cum, run = [], 0
    for k in keys:
        run += values[k]
        cum.append(run)
    total = run
    return lambda: keys[bisect.bisect_left(cum, rng.random() * total)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=8126)
    p.add_argument("--sample", choices=("head",), default="head")
    p.add_argument("--mode", choices=("cumulative", "return_to_go"), default="cumulative")
    p.add_argument("--noise-seed", type=int, default=20260820)
    p.add_argument("--agree", type=float, default=0.62,
                   help="share of rows where the fake column equals reward exactly")
    p.add_argument("--utility-noise", type=float, default=6.0,
                   help="sd of the utility_syn perturbation, in raw utility points")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)

    src = list(csv.DictReader(SRC.open(encoding="utf-8")))
    picked = src[:a.rows]

    eps = defaultdict(list)
    for i, r in enumerate(picked):
        eps[(r["seed"], r["shift_id"], r["agent"])].append(i)

    acc = [0.0] * len(picked)
    for idxs in eps.values():
        if a.mode == "cumulative":
            run = 0.0
            for i in idxs:
                run += float(picked[i]["reward"])
                acc[i] = run
        else:
            run = 0.0
            for i in reversed(idxs):
                run = float(picked[i]["reward"]) + GAMMA * run
                acc[i] = run

    # ---- synthetic_data_rewards ----------------------------------------------------------
    real = [float(r["reward"]) for r in picked]
    marginal = Counter(real)
    by_regime = defaultdict(Counter)
    for v, n in marginal.items():
        by_regime[regime(v)][v] = n

    rng = random.Random(a.noise_seed)
    draw_any = sampler(marginal, rng)
    draw_within = {g: sampler(c, rng) for g, c in by_regime.items()}

    # Three tiers, so the disagreement has structure instead of being uniform noise: mostly
    # agree, sometimes differ in magnitude within the same regime, rarely flip the regime
    # outright. That last tier is what stops the comparison from looking artificially clean --
    # two real models do disagree about whether a decision was good, not only about by how much.
    cross = (1.0 - a.agree) * 0.25
    fake = [0.0] * len(picked)
    for i, v in enumerate(real):
        u = rng.random()
        if u < a.agree:
            fake[i] = v
        elif u < a.agree + cross:
            fake[i] = draw_any()
        else:
            fake[i] = draw_within[regime(v)]()

    by_auction = defaultdict(list)
    for i, r in enumerate(picked):
        by_auction[r["auction_id"]].append(i)

    # ---- utility_q / utility_syn ---------------------------------------------------------
    util_q = [float(r["utility"]) for r in picked]
    lo, hi = min(util_q), max(util_q)
    # Gaussian rather than the reward column's resample-from-empirical: utility is continuous,
    # and what has to move is the ORDER of bidders inside an auction, which a marginal-matching
    # resample would scramble globally instead of perturbing locally.
    util_syn = [min(hi, max(lo, u + rng.gauss(0.0, a.utility_noise))) for u in util_q]

    # ONE WINNER PER AUCTION, repaired rather than hoped for. Perturbing rows independently
    # lets two bidders in the same auction both clear the win floor, which is not a rare draw
    # to tolerate -- it is a state the mechanism cannot produce, and allocation efficiency is
    # measured PER AUCTION, so a two-winner auction silently makes `awarded` count a row that
    # `ranked_ok` then judges against a winner picked arbitrarily. The real column is 0 or 1
    # winners in every one of its 2828 auctions; the fake one has to be too.
    below = Counter({v: c for v, c in marginal.items() if v < WIN_FLOOR})
    draw_below = sampler(below, rng)
    multi = 0
    for idxs in by_auction.values():
        won = [i for i in idxs if fake[i] >= WIN_FLOOR]
        if len(won) < 2:
            continue
        multi += 1
        keep = max(won, key=lambda i: fake[i])
        for i in won:
            if i != keep:
                fake[i] = draw_below()

    dst = Path(a.out) if a.out else OUT / f"output.gate.{a.sample}.{a.rows}rows.csv"
    with dst.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        # agent / candidate_id repeat the input file's columns on purpose: an output
        # file that cannot be read without its input is a footgun when the two drift.
        w.writerow(["row_id", "auction_id", "seed", "shift_id", "agent", "candidate_id",
                    "q_action", "reward", "synthetic_data_rewards",
                    "utility_q", "utility_syn"])
        for i, r in enumerate(picked, start=1):
            w.writerow([i, r["auction_id"], r["seed"], r["shift_id"], r["agent"],
                        r["candidate_id"], r["q_action"], r["reward"], f"{fake[i - 1]:.1f}",
                        f"{util_q[i - 1]:.6f}", f"{util_syn[i - 1]:.6f}"])

    trunc = sum(1 for k, idxs in eps.items()
                if picked[idxs[-1]]["terminal"].lower() != "true")
    mix = defaultdict(int)
    for r in picked: mix[r["q_action"]] += 1
    print(f"source            {SRC.name}")
    print(f"written           {dst.name}   {len(picked)} rows, 11 columns   {dst.stat().st_size:,} bytes")
    print(f"  mode            {a.mode}" + ("   gamma 0.99" if a.mode == "return_to_go" else ""))
    print(f"  episodes        {len(eps)}   grouped by (seed, shift_id, agent)")
    print(f"  TRUNCATED       {trunc} of {len(eps)} episodes end without terminal=True")
    print(f"  accumulated     min {min(acc):.1f}   max {max(acc):.1f}   mean {sum(acc)/len(acc):.1f}")
    print("  q_action mix    " + "  ".join(f"{k} {v} ({v/len(picked):.1%})"
                                           for k, v in sorted(mix.items(), key=lambda kv: -kv[1])))

    n = len(picked)
    same = sum(1 for x, y in zip(real, fake) if x == y)
    flip = sum(1 for x, y in zip(real, fake) if regime(x) != regime(y))
    mr, mf = sum(real) / n, sum(fake) / n
    num = sum((x - mr) * (y - mf) for x, y in zip(real, fake))
    den = (sum((x - mr) ** 2 for x in real) * sum((y - mf) ** 2 for y in fake)) ** 0.5
    print("")
    print(f"synthetic_data_rewards   FAKE second-model column, seed {a.noise_seed}")
    print(f"  reward          min {min(real):7.1f}  max {max(real):7.1f}  mean {mr:7.1f}   "
          f"{len(set(real))} distinct")
    print(f"  synthetic       min {min(fake):7.1f}  max {max(fake):7.1f}  mean {mf:7.1f}   "
          f"{len(set(fake))} distinct")
    print(f"  identical       {same} of {n} rows ({same/n:.1%})")
    print(f"  regime flipped  {flip} of {n} rows ({flip/n:.1%})    won >= 100 > mid >= 0 > lost")
    print(f"  correlation     {num/den:.3f}       mean delta {mf - mr:+.1f}")
    wpa = Counter(sum(1 for i in idxs if fake[i] >= WIN_FLOOR) for idxs in by_auction.values())
    real_wpa = Counter(sum(1 for i in idxs if real[i] >= WIN_FLOOR) for idxs in by_auction.values())
    print(f"  winners/auction reward {dict(sorted(real_wpa.items()))}   "
          f"synthetic {dict(sorted(wpa.items()))}    ({multi} auctions repaired to one winner)")
    print("  NOT A MODEL OUTPUT. Use it to check that a metric pipeline runs and that two")
    print("  columns give two different answers. Never quote the numbers themselves.")

    # ---- the dry run this file exists for, computed here so it is verified, not promised ---
    mu, mv = sum(util_q) / n, sum(util_syn) / n
    unum = sum((x - mu) * (y - mv) for x, y in zip(util_q, util_syn))
    uden = (sum((x - mu) ** 2 for x in util_q) * sum((y - mv) ** 2 for y in util_syn)) ** 0.5

    def efficiency(rewards, utils):
        auctions = awarded = ranked_ok = 0
        for idxs in by_auction.values():
            auctions += 1
            won = [i for i in idxs if rewards[i] >= WIN_FLOOR]
            if not won:
                continue
            awarded += 1
            if picked[won[0]]["agent"] == picked[max(idxs, key=lambda i: utils[i])]["agent"]:
                ranked_ok += 1
        return auctions, awarded, ranked_ok

    flips = sum(1 for idxs in by_auction.values()
                if max(idxs, key=lambda i: util_q[i]) != max(idxs, key=lambda i: util_syn[i]))
    print("")
    print(f"utility_q / utility_syn   utility noise sd {a.utility_noise:.1f} raw points")
    print(f"  utility_q       min {min(util_q):7.2f}  max {max(util_q):7.2f}  mean {mu:7.2f}")
    print(f"  utility_syn     min {min(util_syn):7.2f}  max {max(util_syn):7.2f}  mean {mv:7.2f}")
    print(f"  correlation     {unum/uden:.3f}")
    print(f"  top bidder moved in {flips} of {len(by_auction)} auctions "
          f"({flips/len(by_auction):.1%})   <- what makes the two efficiencies differ")
    print("")
    print(f"DRY RUN  allocation efficiency = ranked_ok / awarded, grouped by auction_id, "
          f"win floor {WIN_FLOOR:.0f}")
    print(f"  {'arm':<26} {'auctions':>9} {'awarded':>9} {'ranked_ok':>10} {'efficiency':>11} "
          f"{'unallocated':>12}")
    for label, rw, ut in (("A  reward / utility_q", real, util_q),
                          ("B  synthetic / utility_syn", fake, util_syn)):
        au, aw, ok = efficiency(rw, ut)
        print(f"  {label:<26} {au:>9} {aw:>9} {ok:>10} {ok/aw:>10.1%} {1 - aw/au:>11.1%}")
    print("  Two arms, two numbers, same code path. That is the whole dry run: if these come")
    print("  out equal the comparison is not measuring anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
