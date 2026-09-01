"""Re-emit the gate validation set in the EXACT schema of artifacts/transitions.jsonl.

Run:  python scripts/export_validation_as_training_schema.py

``validation.gate.seeds101-200.csv`` was written for a human to open: 63 flat columns, the 22
state features exploded into named columns, plus ``seed`` and ``world``. That is not the shape
the training corpus has, so the two files cannot be diffed, concatenated, or fed to the same
loader.

This script writes the same data in the training corpus's shape instead:

  * ER rows only            -> 8126 rows, matching the agent the Q arm actually fits
  * the 20 training keys    -> in transitions.jsonl's key ORDER, from Transition.as_dict()
  * ``state`` as a LIST     -> not 22 separate columns
  * no ``seed`` / ``world`` -> those keys do not exist in the training schema

Provenance moves to the header line, exactly as transitions.jsonl does it (that file records
one ``seed: 7000`` for a 40-seed corpus). Losing the per-row seed is a real cost -- it is the
gap the earlier export existed to close -- so the header states the band explicitly.

The check that matters is at the bottom: the output is loaded back through
``qlearn.load_transitions``, the same function ``scripts/train_q.py`` uses. If it parses, the
format is not merely similar, it is interchangeable.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.contracts import AgentKind
from allocation.rl.qlearn import load_transitions

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"

SRC = OUT / "validation.gate.seeds101-200.jsonl"
DST = OUT / "validation.gate.er.trainingschema.jsonl"
DST_CSV = OUT / "validation.gate.er.trainingschema.csv"
TRAIN = OUT / "transitions.jsonl"

#: Transition.as_dict()'s key order, dataset.py:100-122. Order matters: the point of this
#: export is byte-level comparability with the training corpus, not just matching key names.
KEYS = [
    "auction_id", "shift_id", "agent", "candidate_id", "state", "q_action", "alpha",
    "won", "bid", "utility", "ceiling", "cost", "reward", "complete", "feasible",
    "budget_remaining", "burn_rate", "next_state", "next_feasible", "terminal",
]


def main() -> int:
    train_header = json.loads(TRAIN.read_text(encoding="utf-8").split("\n", 1)[0])
    src_lines = SRC.read_text(encoding="utf-8").splitlines()
    src_header = json.loads(src_lines[0])

    rows = []
    dropped_agents = {}
    for line in src_lines[1:]:
        row = json.loads(line)
        if row["agent"] != AgentKind.ER.value:
            dropped_agents[row["agent"]] = dropped_agents.get(row["agent"], 0) + 1
            continue
        rows.append({k: row[k] for k in KEYS})

    # Header mirrors transitions.jsonl's shape: the three versions, then the run's identity.
    # transitions.jsonl records a single `seed`; this set spans a band, so the band is named
    # and `seed` is null rather than a misleading single value.
    header = {
        "_header": True,
        "caps_version": train_header["caps_version"],
        "encoder_version": src_header["encoder_version"],
        "fabrication_version": src_header["fabrication_version"],
        "seed": None,
        "seeds": src_header["seeds"],
        "n_seeds": src_header["n_seeds"],
        "shifts": src_header["shifts"],
        "agent": AgentKind.ER.value,
        "auctions": src_header["auctions"],
        "completeness": 1.0,
        "role": "VALIDATION - never trained on. Disjoint from training seeds 7000-7039 "
                "(offline Q), 1000-2069 (online Q) and 11-18 (PPO/CEM).",
        "schema": "identical to transitions.jsonl (Transition.as_dict, dataset.py:100-122)",
        "written": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    }

    with DST.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(header) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    # CSV twin with the SAME 20 columns in the SAME order. state / next_state stay as one
    # cell each so the column list matches the JSONL keys exactly.
    with DST_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=KEYS)
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            flat["state"] = json.dumps(row["state"])
            flat["next_state"] = json.dumps(row["next_state"]) if row["next_state"] else ""
            flat["feasible"] = "|".join(row["feasible"])
            flat["next_feasible"] = "|".join(row["next_feasible"])
            writer.writerow(flat)

    print(f"source          {SRC.name}   {len(src_lines) - 1} rows, all agents")
    print(f"  dropped       {dropped_agents}")
    print(f"written         {DST.name}   {len(rows)} rows   "
          f"{DST.stat().st_size:,} bytes")
    print(f"                {DST_CSV.name}   {DST_CSV.stat().st_size:,} bytes")
    print()

    # ---- the check: does the training loader accept it? -------------------------------
    print("VERIFY - schema interchangeability")
    va, vh = load_transitions(str(DST), agent=AgentKind.ER)
    tr, th = load_transitions(str(TRAIN), agent=AgentKind.ER)
    print(f"  load_transitions(validation)  -> {len(va)} Transition objects, OK")
    print(f"  load_transitions(training)    -> {len(tr)} Transition objects, OK")
    print(f"  same encoder_version          -> {vh['encoder_version'] == th['encoder_version']}"
          f"  ({vh['encoder_version']})")
    print(f"  same fabrication_version      -> "
          f"{vh['fabrication_version'] == th['fabrication_version']}")

    first_v = json.loads(DST.read_text(encoding="utf-8").splitlines()[1])
    first_t = json.loads(TRAIN.read_text(encoding="utf-8").splitlines()[1])
    print(f"  identical key LIST and ORDER  -> {list(first_v) == list(first_t)}")
    print(f"  key count                     -> {len(first_v)} vs {len(first_t)}")
    print(f"  state length                  -> {len(first_v['state'])} vs "
          f"{len(first_t['state'])}")
    same_types = all(
        type(first_v[k]) is type(first_t[k]) or None in (first_v[k], first_t[k])
        for k in first_v
    )
    print(f"  identical field types         -> {same_types}")
    print()
    print("  The validation file is now drop-in for anything that reads the training corpus,")
    print("  train_q.py included. Per-row seed is GONE - it is not a training-schema field;")
    print("  the band lives in the header, same limitation transitions.jsonl has.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
