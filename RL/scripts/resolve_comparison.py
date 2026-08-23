"""Run the paired comparison at whatever sample size resolves it.

Run:  python scripts/resolve_comparison.py [n_seeds] [--seed-start 101]

``evaluate_er.py`` runs all three gates at a sample sized for a routine check. When its report
says NOT RESOLVED it also prints how many paired shifts the observed effect would need — this
script exists to actually collect them, without re-running the sweep and shadow gates that do
not depend on the sample.

Held-out seeds only, disjoint from every seed used in training (11-18) and from the sweep's
(201-204). ``RESERVED`` below enforces that rather than trusting the caller.

**Two held-out ranges, and the distinction is the point of ``--seed-start``.** Seeds 101-200 were
used to *choose* between cells B, C, A, the reference and D — ten candidates scored on one sample,
so the surviving figure is the maximum of ten draws and carries selection with it. That makes
101-200 a validation set, whatever the docstring above it once called it. A range never looked at
is the only thing that can carry a reported number, so:

  * ``--seed-start 101`` (default) — the SELECTION range. Use it for every ablation cell, because
    comparability with D's +9.6% requires the same sample.
  * ``--seed-start 301`` — the CONFIRMATION range. Score it ONCE, on the winner, and do not use
    what it says to pick anything. The moment a second candidate is scored here it becomes
    another validation set and a third range is needed.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.evaluate import compare
from allocation.rl.policy import QWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "artifacts" / "comparison_resolved.log"
WEIGHTS = ROOT / "artifacts" / "er_policy.json"

#: Seeds that are NOT held out, and why. ``--seed-start`` makes the range a caller's choice, so
#: the guarantee in this module's docstring stops being a convention and has to be checked: a
#: comparison that silently included a training seed would report memorisation as generalisation.
RESERVED = {"CEM fitness": range(11, 19), "fabrication sweep": range(201, 205)}


def main(argv: list[str]) -> int:
    # --weights / --out added so an ablation cell can be scored at THIS sample size. The 24-seed
    # harness in evaluate_er.py gives 165 paired shifts, which is the sample that reported the
    # current policy at +4.5% / t=1.04 and called it unresolved; the same weights read +5.7% /
    # t=2.92 over the 689 shifts here. Cells compared across different sample sizes are not
    # comparable, so every ablation cell must be scored through this script.
    args = list(argv)
    weights_path, out_path, seed_start = WEIGHTS, LOG, 101
    for flag in ("--weights", "--out", "--seed-start"):
        if flag in args:
            i = args.index(flag)
            raw = args[i + 1]
            if flag == "--weights":
                weights_path = Path(raw)
            elif flag == "--out":
                out_path = Path(raw)
            else:
                seed_start = int(raw)
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

    log = out_path.open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()

    config = _with_base(load_config(), 120.0)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    weights = QWeights.load(weights_path)

    say(f"started              {datetime.now():%H:%M:%S}")
    say(f"policy               {weights_path.name}")
    say(f"encoder / fabrication {weights.encoder_version} / {weights.fabrication_version}")
    role = "selection" if seed_start == 101 else "confirmation"
    say(f"seeds                {n_seeds} held-out ({seeds[0]}-{seeds[-1]}), 6 shifts each")
    say(f"range role           {role}")
    say("")

    result = compare(
        config, weights, agent=AgentKind.ER, seeds=seeds, shifts=6, fab=fab,
    )

    say(result.report())
    say("")
    say(f"finished             {datetime.now():%H:%M:%S}")
    log.close()
    print(result.report())
    return 0 if result.resolved else 3


if __name__ == "__main__":
    print(f"logging to {LOG}", flush=True)
    raise SystemExit(main(sys.argv[1:]))
