"""The falsification sweep that can actually fail: refit under each perturbed constant.

Run:  python scripts/sweep_refit.py

``scripts/evaluate_er.py``'s sweep re-runs *frozen* weights under perturbed constants and always
reports 0.0 % movement. That is structural, not a bug: every ``outcome.*`` constant is read
inside ``sim/outcomes.py``, which prices an episode after the auctions have run, and none is read
by ``sim/world.py``, ``sim/patients.py`` or the auction loop. An outcome constant cannot reach
the state a policy sees, so frozen weights are invariant to it by construction.

The outcome constants are the **objective**. So the question is not whether one policy behaves
differently under them — it cannot — but whether *training* under them produces a different
policy. If nudging a constant nobody measured by 20 % yields a materially different action mix,
the previous fit learned that constant, and its advantage is an artefact of this simulator.

Expensive: one full CEM run per perturbation, plus a baseline and a control — 16 fits at the
default constants. The control is a second fit of the SAME world under a different CEM seed, and
it sets the noise floor. Without it a refit sweep cannot separate "the perturbation changed the
policy" from "the optimiser landed somewhere else", and every row would be unreadable.

The budget below is deliberately smaller than a production fit. This measures *sensitivity of the
fitting process* to the objective, not the quality of any one policy.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.evaluate import refit_sweep
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "artifacts" / "sweep_refit.log"


def main() -> int:
    (ROOT / "artifacts").mkdir(exist_ok=True)
    log = LOG.open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()

    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    total = len(fab.outcome_constants) * 2 + 2

    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"fabrication_version  {fab.version}")
    say(f"fits to run          {total}  (baseline + control + "
        f"{len(fab.outcome_constants)} constants x 2 factors)")
    say("")

    done = [0]

    def on_step(name: str, factor: float) -> None:
        done[0] += 1
        say(f"  [{done[0]:>2}/{total}] fitting {name} x{factor:.2f}  "
            f"{datetime.now():%H:%M:%S}")

    sweep = refit_sweep(
        config,
        agent=AgentKind.ER,
        train_seeds=(11, 12, 13, 14),
        eval_seeds=(201, 202, 203, 204),
        shifts=4,
        generations=6,
        population=12,
        fab=fab,
        on_step=on_step,
    )

    say("")
    say(sweep.report())
    say("")
    say(f"finished             {datetime.now():%H:%M:%S}")
    log.close()
    print(sweep.report())
    return 0 if sweep.passed else 3


if __name__ == "__main__":
    print(f"logging to {LOG}", flush=True)
    raise SystemExit(main())
