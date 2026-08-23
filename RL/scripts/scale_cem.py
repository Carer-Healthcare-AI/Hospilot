"""One CEM budget cell, at whatever population and generations you name.

Run:  python scripts/scale_cem.py --population 64 --label E_896ev_pop64

The 2x2 in ``artifacts/sweep_cem_budget.log`` established that evaluation count is the lever and
``sigma_floor`` is incidental: 72 -> 336 evaluations moved the held-out return ~34 points at both
floor settings, while a 50x change in the floor moved it under 2 points at both budgets. Cell D
(48x14 = 672) then answered whether the curve keeps paying: **+9.6% at t=5.08 against the
reference's +5.7% at t=2.92**, so it does, and the curve has not flattened.

**Population, not generations, and that is still the axis.** By generation 13 of the 336-eval cell
sigma was 0.012 — the sampling distribution had collapsed and the last generations were sampling a
near-point, so adding generations to a converged distribution buys nothing. What is thin is samples
per dimension: 72 evaluations over 161 parameters is under half an evaluation per dimension, 336
gives ~2, 672 gives ~4. With ``elite_fraction`` 0.25 a larger population also enlarges the elite,
and the elite is what the per-parameter standard deviation is refitted from — six samples to
estimate 161 spreads is the noisiest part of the whole loop. D's elite of 12 disagreed more and
held sigma at 0.067 by generation 11 against the reference's 0.056, i.e. it kept searching where
the reference had begun to converge.

**But that argument expires, and the sigma trace is what tells you when.** It rests on the
distribution being collapsed by the final generation. A larger elite holds sigma wider for longer,
so at some population 14 generations stops being generous and starts being a truncation — at which
point a cell that lands flat has measured the generation cap, not the budget curve. So before
choosing the next cell, read ``sigma`` on the last generation of this one:

  * final sigma well under ~0.05  -> the distribution converged inside 14 generations. The budget
    axis is still population; raise it.
  * final sigma at or above ~0.05 -> the search was still moving when it ran out of generations.
    Raise ``--generations``, not ``--population``, or the next cell answers the wrong question.

**Reading the return.** Against cell D's 782.25 / +9.6% / t=5.08, not against the reference:

  * >=2 points over D with t rising -> the curve is still paying. 2 points is the noise scale the
    2x2 measured for a variable *known* to be irrelevant (``sigma_floor``), so a smaller gain is
    not distinguishable from changing nothing.
  * near or below D -> the linear representation is binding rather than the search budget, and
    further CEM spend on 161 parameters is wasted. That is the point to add capacity (CEM on a
    small MLP first, sized so evaluations-per-parameter matches what worked here — ~4).

**``noaward`` is a rejection criterion, not a metric.** The reward does not price an unallocated
bed, so return is buyable by leaving beds empty, and the reference did exactly that: 6.4% -> 9.1%
while its return rose. D pulled it back to 7.5%. A cell above 7.5% is rejected whatever its return
says, because the thing it improved is not the thing being measured.

**Nothing here is edited between runs.** Every swept value is an argument and the label defaults to
one derived from the config, because cell C's original 72-evaluation configuration is unrecoverable
for exactly that reason: ``train_er.py`` was edited after producing its own log, and now reads
``population=16`` while ``training.2seed.log`` shows ``5/12``. A script that has to be edited to
sweep cannot say afterwards what it swept.
"""

from __future__ import annotations

import argparse
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

#: Held fixed across every cell, so a cell is comparable to D by construction. These are
#: ``run_training.py``'s values; changing one silently makes the whole ablation series
#: incomparable, which is why they are constants and the swept variables are arguments.
SEEDS = (11, 12, 13, 14, 15, 16, 17, 18)
SHIFTS = 4
BASE = 120.0
PARAM_COUNT = 161

#: Cell D, the incumbent every new cell is read against. From artifacts/comparison.D_672ev_pop48.log.
D_RETURN, D_DELTA, D_T, D_NOAWARD = 782.25, 0.096, 5.08, 0.075

#: Seconds per evaluation at 8 seeds x 4 shifts, measured over the D run. Used only to print an
#: estimate before committing an hour, and to make it obvious that these cells must run
#: SEQUENTIALLY — the 2026-08-16 logs show concurrent runs roughly tripling each other's
#: per-generation time.
SECONDS_PER_EVAL = 5.9


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--population", type=int, default=48)
    parser.add_argument("--generations", type=int, default=14)
    parser.add_argument("--sigma-floor", type=float, default=0.05)
    parser.add_argument(
        "--cem-seed", type=int, default=0,
        help="RNG seed for the CEM search itself (train(seed=...)). Cells B, C, A, the "
             "reference, D and E all ran at the default 0, which is why the budget series is "
             "n=1 per level and cannot separate a budget effect from run-to-run variance.",
    )
    parser.add_argument(
        "--label", default=None,
        help="artifact stem. Defaults to one derived from the config, so a cell can never "
             "overwrite another cell's weights under a name that describes a different run.",
    )
    parser.add_argument(
        "--score-seed-start", type=int, default=101,
        help="101 = the selection range, comparable with D. Use 301 only to confirm a winner.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing weights file for this label.",
    )
    args = parser.parse_args(argv)

    evals = args.population * args.generations
    label = args.label or (
        f"pop{args.population}x{args.generations}_{evals}ev"
        + (f"_s{args.cem_seed}" if args.cem_seed else "")
    )

    weights_path = OUT / f"er_policy.{label}.json"
    if weights_path.exists() and not args.force:
        print(
            f"REFUSING: {weights_path.name} already exists. Pass --label for a new cell, or "
            "--force to overwrite. A cell that overwrites another cell's weights leaves a log "
            "describing a run the file no longer holds."
        )
        return 2

    log = (OUT / f"scale_cem.{label}.log").open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()
        print(text, flush=True)

    config = _with_base(load_config(), BASE)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })

    estimate = evals * SECONDS_PER_EVAL / 60.0
    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"fabrication_version  {fab.version}")
    say(f"cell                 {label}: {args.population}x{args.generations} = {evals} evals "
        f"({evals / PARAM_COUNT:.1f} per parameter), sigma_floor {args.sigma_floor:g}")
    say(f"cem_seed             {args.cem_seed}")
    say(f"estimate             ~{estimate:.0f} min at {SECONDS_PER_EVAL:g} s/eval — but the "
        f"observed rate spans 3.4-8.4 s/eval with machine load, so treat this as a lower bound")
    say(f"incumbent            cell D  48x14 = 672 evals, return {D_RETURN:.2f}, "
        f"{D_DELTA:+.1%} at t={D_T:.2f}, noaward {D_NOAWARD:.1%}")
    say(f"accept if            return >= {D_RETURN + 2.0:.2f} (D + 2, the sigma_floor noise "
        f"scale) AND noaward <= {D_NOAWARD:.1%} AND abandonments == 0")
    say(f"held fixed           agent er, seeds {list(SEEDS)}, shifts {SHIFTS}, Base {BASE:g}")
    say("")

    run = train(
        config,
        agent=AgentKind.ER,
        generations=args.generations,
        population=args.population,
        sigma_floor=args.sigma_floor,
        seeds=SEEDS,
        shifts=SHIFTS,
        seed=args.cem_seed,
        fab=fab,
        on_generation=lambda g: say(
            f"  gen {g.index:>3} best {g.best_fitness:>8.1f} elite {g.elite_mean:>8.1f} "
            f"sigma {g.sigma:>6.3f} burn {g.burn:>6.1%} aband {g.abandonments:>3} "
            f"feas {g.feasible:>3}/{g.population:<3} {datetime.now():%H:%M:%S}"
        ),
        checkpoint=str(OUT / f"er_policy.{label}.checkpoint.json"),
    )

    run.weights.save(weights_path)
    say("")
    say(run.report())
    say(f"  weights -> {weights_path.name}   done {datetime.now():%H:%M:%S}")

    comparison_log = OUT / f"comparison.{label}.log"
    say(f"  scoring at 100 seeds / 689 shifts from seed {args.score_seed_start} "
        f"-> {comparison_log.name}")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "resolve_comparison.py"),
         "--weights", str(weights_path), "--out", str(comparison_log),
         "--seed-start", str(args.score_seed_start), "100"],
        capture_output=True, text=True,
    )
    if result.returncode == 2:
        say(f"  SCORING REFUSED: {result.stdout.strip()[-400:]}")
    elif result.returncode not in (0, 3):
        say(f"  SCORING FAILED rc={result.returncode}: {result.stderr[-400:]}")
    else:
        for line in comparison_log.read_text(encoding="utf-8").splitlines():
            if any(k in line for k in
                   ("vs heuristic on identical", "t = mean", "RESOLVED", "shifts compared",
                    "rl-linear", "heuristic  ", "learned better on")):
                say(f"    {line.rstrip()}")

    say("")
    say(f"finished             {datetime.now():%H:%M:%S}")
    say("Cell E (64x14 = 896, cem_seed 0) scored 758.82 / +6.3% / t=3.16, i.e. 23 points BELOW")
    say("cell D at 672 evals, with noaward back up to 9.3%. It matched the 336-eval reference on")
    say("every behavioural column (burn 46.6 vs 46.5, rank 84.0 vs 84.4, noaward 9.3 vs 9.1) and")
    say("fit WORSE in training on 33% more evaluations. So a budget cell is not the run to do")
    say("next: every cell in the series ran at cem_seed 0, which makes each budget n=1, and the")
    say("+5.7 / +9.6 / +6.3 spread over 336 / 672 / 896 is consistent with the budget doing")
    say("nothing above 336 while CEM run-to-run variance is worth ~12 points. Replicate D at")
    say("--cem-seed 1 and 2 before spending anything on a new budget or a new architecture; the")
    say("run-to-run spread is also what an MLP result would have to beat to mean anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
