"""Run the paired validation and persist EVERY per-shift metric, not just the aggregates.

Run:  python scripts/export_validation.py [n_seeds] [--weights P] [--seed-start 101] [--shifts 6]

``resolve_comparison.py`` already runs this comparison, but it writes only the summary block:
two metric rows plus mean/sd/se/t. The 689 individual paired differences behind those numbers
are computed in ``Comparison.paired_diffs`` and then discarded, which makes a completed run
un-reanalysable — you cannot plot the distribution, find the outliers driving the mean, ask
whether the gain concentrates in night shifts, or run a non-parametric test. Re-running is the
only option and it costs an hour.

That gap hid something real. On the confirmation range the policy is better on 371/689 shifts
(53.8%, sign-test p=0.044) while the magnitude test reads t=5.32 — so the improvement is carried
by a minority of large recoveries rather than by broadly better decisions. Nothing in the summary
block says that; it only surfaces by cross-checking the better-on count against the t. A run that
saved its per-shift rows would have shown it directly.

**Two files, and the split matters.** The ``.csv`` is one row per paired shift and is the artifact
worth keeping: it is the validation set's *results*, which is the closest thing this project has to
a stored validation set at all (the set itself is five lines of config — a seed range, a shift
count, a Base and a fabrication hash — and the worlds are regenerated on demand). The ``.log``
carries the same aggregates ``resolve_comparison`` prints, so the two stay comparable.

Per-shift metrics are computed for BOTH policies on the same shift, so every column can be paired.
``rank`` / ``pinned`` / ``noaward`` are auction-level counts and are emitted as their raw
numerator and denominator rather than as ratios: a shift with two awarded auctions has a
ranking-respect of 0%, 50% or 100% and averaging those across shifts is not the same number as
the pooled ratio the summary reports. Keeping the counts lets the reader pool them correctly.

Seed hygiene is inherited from ``resolve_comparison.RESERVED`` rather than re-declared, so a
range that would measure memorisation is refused here for the same reason and by the same table.
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.policy import QWeights
from allocation.rl.train import MixedPolicy
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

from resolve_comparison import RESERVED

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"

#: Same thresholds ``rl/evaluate.measure`` uses for the ``pinned`` count, kept as named constants
#: because two definitions of "pinned" that drift apart would make this file's numbers quietly
#: disagree with the summary block it exists to explain.
PINNED_BID_FRACTION = 0.9
PINNED_BURN_FLOOR = 0.6

FIELDS = [
    "seed", "shift_id",
    "h_return", "r_return", "diff",
    "h_auctions", "r_auctions",
    "h_wins", "r_wins",
    "h_spend", "r_spend",
    "h_burn", "r_burn",
    "h_awarded", "r_awarded",
    "h_rank_ok", "r_rank_ok",
    "h_pinned", "r_pinned",
    "h_noaward", "r_noaward",
    "h_groups", "r_groups",
]


def _per_shift(dataset, agent: AgentKind) -> dict[str, dict[str, float]]:
    """Every metric the summary pools, kept per shift for one policy's run.

    Episodes carry the return and the spend; the auction-level counts have to come off the
    transitions, because ``Step`` records one agent's view and ranking respect is a statement
    about which of *all* the bidders won.
    """
    out: dict[str, dict[str, float]] = {}

    for episode in dataset.complete_episodes:
        if episode.agent is not agent:
            continue
        out[episode.shift_id] = {
            "return": episode.discounted_return,
            "auctions": float(len(episode.steps)),
            "wins": float(episode.wins),
            "spend": episode.spend,
            "burn": 0.0,
            "groups": 0.0, "awarded": 0.0, "rank_ok": 0.0, "pinned": 0.0, "noaward": 0.0,
        }

    burns: dict[str, list[float]] = {}
    groups: dict[str, dict[str, list]] = {}
    for t in dataset.transitions:
        if t.shift_id not in out:
            continue
        if t.agent is agent:
            burns.setdefault(t.shift_id, []).append(t.burn_rate)
        groups.setdefault(t.shift_id, {}).setdefault(t.auction_id, []).append(t)

    for shift_id, row in out.items():
        seen = burns.get(shift_id, [])
        row["burn"] = statistics.fmean(seen) if seen else 0.0
        for group in groups.get(shift_id, {}).values():
            row["groups"] += 1
            won = [t for t in group if t.won]
            if not won:
                row["noaward"] += 1
                continue
            row["awarded"] += 1
            if won[0].agent is max(group, key=lambda t: t.utility).agent:
                row["rank_ok"] += 1
            if (
                won[0].ceiling > 0
                and won[0].bid < won[0].ceiling * PINNED_BID_FRACTION
                and won[0].burn_rate > PINNED_BURN_FLOOR
            ):
                row["pinned"] += 1
    return out


def main(argv: list[str]) -> int:
    args = list(argv)
    weights_path = OUT / "er_policy.json"
    seed_start, shifts, tag = 101, 6, None
    for flag in ("--weights", "--seed-start", "--shifts", "--tag"):
        if flag in args:
            i = args.index(flag)
            raw = args[i + 1]
            if flag == "--weights":
                weights_path = Path(raw)
            elif flag == "--seed-start":
                seed_start = int(raw)
            elif flag == "--shifts":
                shifts = int(raw)
            else:
                tag = raw
            del args[i:i + 2]
    n_seeds = int(args[0]) if args else 100

    seeds = tuple(range(seed_start, seed_start + n_seeds))
    for purpose, reserved in RESERVED.items():
        clash = sorted(set(seeds) & set(reserved))
        if clash:
            print(
                f"REFUSING: seeds {clash[0]}-{clash[-1]} are already used for {purpose} "
                f"({reserved.start}-{reserved.stop - 1}). A comparison on them measures "
                "memorisation, not generalisation."
            )
            return 2

    stem = tag or f"{weights_path.stem}.s{seed_start}"
    csv_path, log_path = OUT / f"validation.{stem}.csv", OUT / f"validation.{stem}.log"
    log = log_path.open("w", encoding="utf-8", buffering=1)

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
    learner = MixedPolicy(config, weights, AgentKind.ER)

    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"policy               {weights_path.name}")
    say(f"encoder / fabrication {weights.encoder_version} / {weights.fabrication_version}")
    say(f"seeds                {n_seeds} ({seeds[0]}-{seeds[-1]}), {shifts} shifts each")
    say(f"range role           {'selection' if seed_start == 101 else 'confirmation'}")
    say(f"per-shift rows       {csv_path.name}")
    say("")

    rows: list[dict[str, float | str | int]] = []
    for index, seed in enumerate(seeds, 1):
        h = _per_shift(generate(config, seed=seed, shifts=shifts, fab=fab), AgentKind.ER)
        r = _per_shift(
            generate(config, seed=seed, shifts=shifts, policy=learner, fab=fab), AgentKind.ER
        )
        for shift_id in sorted(set(h) & set(r)):
            a, b = h[shift_id], r[shift_id]
            rows.append({
                "seed": seed, "shift_id": shift_id,
                "diff": round(b["return"] - a["return"], 4),
                **{f"h_{k}": round(v, 4) for k, v in a.items()},
                **{f"r_{k}": round(v, 4) for k, v in b.items()},
            })
        if index % 10 == 0:
            say(f"  seed {index:>3}/{n_seeds}   {len(rows):>5} paired shifts")

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    diffs = [float(row["diff"]) for row in rows]
    n = len(diffs)
    if n < 2:
        say("")
        say(f"only {n} paired shifts — nothing to summarise. CSV written anyway.")
        log.close()
        return 3

    mean = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    se = sd / math.sqrt(n)
    better = sum(1 for d in diffs if d > 0)
    # Sign test alongside the t. The two answer different questions and the confirmation run
    # showed them disagreeing sharply: t=5.32 on magnitude against p=0.044 on frequency, which
    # is what "carried by a minority of large recoveries" looks like in numbers.
    z = (better - n / 2) / math.sqrt(n * 0.25)
    h_mean = statistics.fmean(float(row["h_return"]) for row in rows)
    r_mean = statistics.fmean(float(row["r_return"]) for row in rows)

    say("")
    say(f"  paired shifts        {n}")
    say(f"  heuristic return     {h_mean:8.2f}")
    say(f"  learned return       {r_mean:8.2f}")
    say(f"  return delta         {(r_mean - h_mean) / abs(h_mean):+8.1%}")
    say("")
    say(f"  mean diff            {mean:+8.2f}")
    say(f"  sd                   {sd:8.2f}")
    say(f"  standard error       {se:8.2f}")
    say(f"  t = mean / se        {mean / se:8.2f}")
    say(f"  better on            {better}/{n} = {better / n:.1%}")
    say(f"  sign-test z          {z:+8.2f}   p = {math.erfc(abs(z) / math.sqrt(2)):.4f}")
    say("")
    if abs(mean / se) >= 2.0:
        say("  RESOLVED on magnitude — the difference is outside the shift-to-shift noise.")
    else:
        needed = int((2 * sd / mean) ** 2) if mean else 0
        say(f"  NOT RESOLVED — |t| < 2. Resolving this effect needs ~{needed} paired shifts.")
    # A large t beside a weak sign test is the asymmetry worth naming rather than leaving the
    # reader to divide two numbers themselves.
    if abs(mean / se) >= 2.0 and abs(z) < 2.0:
        say("  ASYMMETRIC — the magnitude test resolves but the frequency test does not: the")
        say("  gain is carried by a minority of shifts with large differences, not by winning")
        say("  more often. Read the CSV's `diff` column before quoting the percentage.")
    say("")
    say(f"finished             {datetime.now():%H:%M:%S}")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
