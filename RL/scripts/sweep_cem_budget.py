"""Separate CEM's two confounded variables: evaluation count and ``sigma_floor``.

Run:  python scripts/sweep_cem_budget.py

RL_FIXES calls the 72-eval run's failure a *search budget* defect and reports 336 evaluations
"with a sigma floor" converging above baseline. Two things moved between those runs, so which one
carries the result is untested. It matters because the answer decides whether buying more compute
buys anything: if ``sigma_floor`` is the lever, a bigger population is close to wasted spend.

                        sigma_floor = 1e-3        sigma_floor = 0.05
    72 evals  (12x6)    cell C                    cell B
    336 evals (24x14)   cell A                    already on disk -> er_policy.json, +5.7%

**Cell C exists on disk in name only and is re-run here.** RL_FIXES reports it at -30.7%, but the
config is not recoverable: there is no ``training.8seed.log``, and ``train_er.py`` — the only
script that fits at 72 evaluations — now reads ``population=16`` while ``training.2seed.log``
shows ``5/12``, i.e. population 12. It has been edited since it produced its own log. A 2x2 whose
control came from an unknown config is not a 2x2, so C is measured rather than cited.

Everything except the two swept variables is held at ``run_training.py``'s values (8 seeds,
shifts=4, Base 120, release 1.8/h), which is what makes the on-disk cell a legitimate fourth
corner.

Each cell is scored through ``resolve_comparison.py`` at 100 held-out seeds / 689 paired shifts.
Not through ``evaluate_er.py``: its 24 seeds give 165 shifts, the sample that read the current
policy as unresolved at t=1.04 when 689 shifts put the same weights at t=2.92.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.train import train
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"

SEEDS = (11, 12, 13, 14, 15, 16, 17, 18)
SHIFTS = 4

# (label, population, generations, sigma_floor)
CELLS = [
    ("B_72ev_sig05", 12, 6, 0.05),
    ("C_72ev_sig001", 12, 6, 1e-3),
    ("A_336ev_sig001", 24, 14, 1e-3),
]


def main() -> int:
    log = (OUT / "sweep_cem_budget.log").open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()
        print(text, flush=True)

    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })

    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"fabrication_version  {fab.version}")
    say(f"held fixed           agent er, seeds {list(SEEDS)}, shifts {SHIFTS}, Base 120")
    say("swept                population x generations, sigma_floor")
    say("fourth corner        er_policy.json (24x14, sigma_floor 0.05) already on disk")
    say("")

    for label, population, generations, sigma_floor in CELLS:
        evals = population * generations
        say(f"=== {label}:  {population}x{generations} = {evals} evals, "
            f"sigma_floor {sigma_floor:g}   start {datetime.now():%H:%M:%S}")

        run = train(
            config,
            agent=AgentKind.ER,
            generations=generations,
            population=population,
            sigma_floor=sigma_floor,
            seeds=SEEDS,
            shifts=SHIFTS,
            fab=fab,
            on_generation=lambda g: say(
                f"  gen {g.index:>3} best {g.best_fitness:>8.1f} elite {g.elite_mean:>8.1f} "
                f"sigma {g.sigma:>6.3f} burn {g.burn:>6.1%} aband {g.abandonments:>3} "
                f"feas {g.feasible:>3}/{g.population:<3} {datetime.now():%H:%M:%S}"
            ),
            checkpoint=str(OUT / f"er_policy.{label}.checkpoint.json"),
        )

        weights_path = OUT / f"er_policy.{label}.json"
        run.weights.save(weights_path)
        say("")
        say(run.report())
        say(f"  weights -> {weights_path.name}   done {datetime.now():%H:%M:%S}")

        # Scored in a subprocess so a comparison that dies cannot lose the fitted weights.
        comparison_log = OUT / f"comparison.{label}.log"
        say(f"  scoring at 100 seeds / 689 shifts -> {comparison_log.name}")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "resolve_comparison.py"),
             "--weights", str(weights_path), "--out", str(comparison_log), "100"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            say(f"  SCORING FAILED rc={result.returncode}: {result.stderr[-400:]}")
        else:
            for line in comparison_log.read_text(encoding="utf-8").splitlines():
                if any(k in line for k in
                       ("vs heuristic on identical", "t = mean", "RESOLVED", "shifts compared")):
                    say(f"    {line.strip()}")
        say("")

    say(f"finished             {datetime.now():%H:%M:%S}")
    say("")
    say("Read the 2x2 before concluding: if B recovers most of the gain the lever is")
    say("sigma_floor and more compute buys little; if A recovers it the lever is eval count;")
    say("if neither, the two interact and both are required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
