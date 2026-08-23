"""Train the Q-function by TD learning, collecting under epsilon-greedy exploration.

Run:  python scripts/train_q_online.py [--agent er] [--rounds 12]

**This is the script ``scripts/train_q.py`` cannot be.** That one fits offline against a fixed
dataset, and the dataset is collected by the deterministic heuristic — so it contains only the
actions the heuristic happens to choose. Measured on the shipped 40-seed corpus that is two of
six for ER (``win_now`` 73 %, ``re_enter_later`` 27 %) and three of six for OT, out of 19,919
transitions. The other rows are never updated, stay at zero, and score exactly 0.0 for every
state. More seeds do not help: the behaviour policy is deterministic, so seed 400 makes the same
choices as seed 1.

Here the collecting policy is the learner itself wrapped in :class:`~allocation.rl.qlearn.
EpsilonGreedy`, which takes a random *feasible* action with probability epsilon. Epsilon decays
from 0.60 to 0.05 across the run — high at the start because at round zero every weight is zero,
so every action ties and the greedy argmax is whichever sorts first.

Why this matters beyond tidiness: a greedy argmax over a Q-function with untrained rows selects
one of those rows whenever every learned action scores below zero. One of them is
``withdraw_unplanned`` — leave the auction with nothing arranged for the patient. An untrained
action is not a neutral abstention, it is a live and arbitrary choice.

**What it still cannot fix, by design.** ``await_next_resource`` is gated on
``P(a bed frees inside the patient's safe window) >= 0.70`` (``config/rules/pathway.yaml``).
ER and Ward patients have windows too short to clear it, so the action is *never feasible* for
them and exploration cannot reach it — :class:`EpsilonGreedy` samples the feasible set only.
That is the pathway model working, not a gap: a deteriorating patient should not be made to wait
on a Poisson estimate derived from a four-hour discharge rate. The coverage table reports it as
``n/a — never feasible`` rather than as a failure.

The two non-default world settings match ``scripts/train_er.py``, so a TD policy and a CEM
policy are trained in the same world and their evaluations are comparable. See that file for
why Base 120 and 1.8 releases/hour.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.qlearn import train_q
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

OUT = Path(__file__).resolve().parents[1] / "artifacts"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="er")
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--shifts", type=int, default=6)
    parser.add_argument("--seeds-per-round", type=int, default=3)
    parser.add_argument("--lr", type=float, default=0.02)
    parser.add_argument("--epsilon-start", type=float, default=0.60)
    parser.add_argument("--epsilon-end", type=float, default=0.05)
    parser.add_argument("--base", type=float, default=120.0)
    parser.add_argument("--release", type=float, default=1.8)
    args = parser.parse_args()

    config = _with_base(load_config(), args.base)
    fab = register({
        "arrival.bed_release_per_hour": args.release,
        "arrival.candidate_per_hour": args.release * 2.0,
    })
    agent = AgentKind(args.agent)

    print(f"fabrication_version {fab.version}   "
          f"(release {args.release:g}/h, Base {args.base:g})")
    print(f"training {agent.value} by TD; the other agents stay on the heuristic")
    print(f"epsilon {args.epsilon_start:g} -> {args.epsilon_end:g} over {args.rounds} rounds\n")

    run = train_q(
        config,
        agent=agent,
        rounds=args.rounds,
        shifts_per_round=args.shifts,
        seeds_per_round=args.seeds_per_round,
        learning_rate=args.lr,
        epsilon_start=args.epsilon_start,
        epsilon_end=args.epsilon_end,
        fab=fab,
        on_round=lambda r: print(
            f"  rnd {r.index:>2}  eps {r.epsilon:.2f}  new {r.collected:>5}  "
            f"buf {r.buffer_size:>6}  td {r.td_error:>7.4f}  "
            f"return {r.mean_return:>8.2f}  burn {r.burn:>5.1%}  "
            f"win {r.win_share:>4.0%}  explored {r.explored:>5.1%}",
            flush=True,
        ),
    )

    print()
    print(run.report())

    OUT.mkdir(exist_ok=True)
    path = run.weights.save(OUT / f"{agent.value}_q_policy.online.json")
    print(f"\nweights written to {path}")

    # Written to a `.online.json` name rather than over `<agent>_q_policy.json`, so the offline
    # fit stays on disk beside it. The whole point of this script is the difference between the
    # two coverage tables, and overwriting would destroy the comparison.
    return 0 if not run.untrained else 4


if __name__ == "__main__":
    raise SystemExit(main())
