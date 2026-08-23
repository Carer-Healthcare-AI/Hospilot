"""Train and write progress straight to a file. No pipes, no buffering games.

Earlier runs were lost to shell plumbing rather than to anything about the learning: output
through ``| tee | tail`` sat in a pipe buffer, and a run moved from foreground to background
mid-flight died after one generation. Writing from inside the process, flushing after every
line, removes the whole class of problem — progress is on disk the moment it happens, and a
killed run leaves everything it had already completed.

Run:  python scripts/run_training.py
Watch: the file it prints on the first line.
"""

from __future__ import annotations

import sys
import traceback
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
LOG = OUT / "training.log"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    log = LOG.open("w", encoding="utf-8", buffering=1)  # line-buffered

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()

    try:
        config = _with_base(load_config(), 120.0)
        fab = register({
            "arrival.bed_release_per_hour": 1.8,
            "arrival.candidate_per_hour": 3.6,
        })

        say(f"started              {datetime.now():%H:%M:%S}")
        say(f"fabrication_version  {fab.version}   (release 1.8/h, Base 120)")
        say("training ER; OT and Ward stay on the heuristic")
        say("reward is PER-AGENT (F-23 fixed) — winners ~+150, losers ~-90")
        say("")
        say(
            f"  {'gen':>3} {'best':>9} {'elite':>9} {'mean':>9} {'burn':>7} {'win':>5} "
            f"{'aband':>5} {'feas':>7}  time"
        )

        def on_generation(g) -> None:
            say(
                f"  {g.index:>3} {g.best_fitness:>9.1f} {g.elite_mean:>9.1f} "
                f"{g.mean_fitness:>9.1f} {g.burn:>7.1%} {g.win_share:>5.0%} "
                f"{g.abandonments:>5} {g.feasible:>3}/{g.population:<3}  "
                f"{datetime.now():%H:%M:%S}"
            )

        run = train(
            config,
            agent=AgentKind.ER,
            # 24 x 14 = 336 evaluations over 161 parameters, against 72 before. CEM needs
            # samples per dimension; at 72 the search converged (sigma 0.379 -> 0.029 by
            # generation 5) onto a policy that under-competes — burn 34% against the
            # heuristic's 55% — which is what an under-resourced search looks like when it
            # stops exploring rather than when it finds an optimum.
            generations=14,
            population=24,
            # Keeps the distribution from collapsing to a point by generation 4.
            sigma_floor=0.05,
            # Eight training seeds, not two. Fitting on two arrival streams produced +5.5% on
            # those seeds and -16.5% on twenty-four held-out ones (t = -4.00) — CEM selects on
            # fitness, so two seeds is two draws of noise for the elite to memorise. Same
            # defect the evaluation had, one level up.
            seeds=(11, 12, 13, 14, 15, 16, 17, 18),
            shifts=4,
            fab=fab,
            on_generation=on_generation,
            # Write the distribution mean every generation. Without this a run that is killed
            # — and the previous one died at generation 3 of 6 — leaves nothing on disk, and
            # the weights that survived had no log to attribute them to.
            checkpoint=str(OUT / "er_policy.checkpoint.json"),
        )

        say("")
        say(run.report())
        path = run.weights.save(OUT / "er_policy.json")
        say("")
        say(f"WEIGHTS WRITTEN {path}")
        say(f"finished             {datetime.now():%H:%M:%S}")
        return 0

    except Exception:
        say("")
        say("FAILED")
        say(traceback.format_exc())
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    print(f"logging to {LOG}", flush=True)
    raise SystemExit(main())
