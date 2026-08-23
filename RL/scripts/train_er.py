"""Train ER's policy against two frozen heuristics, at a Base where the budget binds.

Run:  python scripts/train_er.py

The two settings that are not defaults, and why:

``common_points = 120`` — the shipped 700 produces 1.7 % burn in this world, an order of
magnitude below AGENT_BUDGET's 0.40 inert threshold. At that level spending is free and the
optimal policy is trivially "bid your ceiling every time"; a run would converge, report a good
return, and have learned nothing about pacing (F-27, RL_READINESS §5.1).

``bed_release_per_hour = 1.8`` — more auctions per shift raises burn without shrinking Base
further. That matters because shrinking Base is what makes the affordability guard bind, and a
bound guard caps bids below ceilings, which is what destroys ranking respect. See the trade-off
table in the run report: 47 % burn at 76 % ranking respect is the best available compromise, and
the 0.70-1.10 working band is not reachable without dropping ranking respect to ~55 %.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.train import train
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

OUT = Path(__file__).resolve().parents[1] / "artifacts"


def main() -> int:
    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })

    print(f"fabrication_version {fab.version}   (release 1.8/h, Base 120)")
    print("training ER; OT and Ward stay on the heuristic\n")

    run = train(
        config,
        agent=AgentKind.ER,
        generations=6,
        population=16,
        seeds=(11, 12),
        shifts=5,
        fab=fab,
        on_generation=lambda g: print(
            f"  gen {g.index}  best {g.best_fitness:8.2f}  elite {g.elite_mean:8.2f}  "
            f"burn {g.burn:5.1%}  win {g.win_share:4.0%}  aband {g.abandonments}  "
            f"feasible {g.feasible}/{g.population}",
            flush=True,
        ),
        checkpoint=str(OUT / "er_policy.checkpoint.json"),
    )

    print()
    print(run.report())

    OUT.mkdir(exist_ok=True)
    path = run.weights.save(OUT / "er_policy.json")
    print(f"\nweights written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
