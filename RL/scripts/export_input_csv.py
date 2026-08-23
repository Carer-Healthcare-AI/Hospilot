"""Cut a model-INPUT csv from the gate validation set, in transitions.jsonl's own schema.

Run:  python scripts/export_input_csv.py [--rows 8126] [--sample head|stratified]

The columns are transitions.jsonl's field NAMES, with transitions.jsonl's MEANINGS -- but the
VALUES are validation worlds (seeds 101-200), which no model trained on.

Only the fields available BEFORE the decision are kept. transitions.jsonl also carries the
decision and everything downstream of it, and an input file holding those would leak the label:

    kept     auction_id shift_id agent candidate_id state utility ceiling
             budget_remaining burn_rate feasible
    dropped  q_action alpha won bid cost reward           <- the answer
             next_state next_feasible terminal complete   <- known only afterwards

NO INVENTED COLUMNS. An earlier version of this file exploded ``state`` into 22 named columns
and added a ``row_id``. Neither exists in transitions.jsonl, and the explosion collided: the
scaled feature at position 0 got the name ``utility``, which transitions.jsonl already uses for
the RAW 0-200 value. Same name, values 200x apart, no error raised. ``state`` is therefore kept
as one JSON list here, exactly as the corpus stores it.

Join to output.gate.*.csv on (auction_id, agent) -- or on row order, which is identical.
"""
from __future__ import annotations
import argparse, csv, json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from allocation.rl.encoder import NAMES

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
SRC = OUT / "validation.gate.seeds101-200.csv"

#: transitions.jsonl field names, in its own key order (dataset.py:100-122), minus the answers.
HEADER = ["auction_id", "shift_id", "agent", "candidate_id", "state",
          "utility", "ceiling", "budget_remaining", "burn_rate", "feasible"]

def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=8126)
    p.add_argument("--sample", choices=("head", "stratified"), default="head")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)

    src = list(csv.DictReader(SRC.open(encoding="utf-8")))
    if a.sample == "head":
        picked = src[:a.rows]
    else:
        by = defaultdict(list)
        for r in src: by[r["agent"]].append(r)
        picked = []
        for agent, rows in by.items():
            want = round(a.rows * len(rows) / len(src))
            step = len(rows) / want if want else 1
            picked += [rows[min(int(i*step), len(rows)-1)] for i in range(want)]
        picked = picked[:a.rows]
        order = {id(r): i for i, r in enumerate(src)}
        picked.sort(key=lambda r: order[id(r)])

    dst = Path(a.out) if a.out else OUT / f"input.gate.{a.sample}.{a.rows}rows.csv"
    with dst.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for r in picked:
            w.writerow({
                "auction_id": r["auction_id"], "shift_id": r["shift_id"],
                "agent": r["agent"], "candidate_id": r["candidate_id"],
                # the 22 scaled features as ONE list, the way the corpus stores them
                "state": json.dumps([float(r[f"s_{n}"]) for n in NAMES]),
                # raw values, same meaning as the corpus's fields of these names
                "utility": r["utility"], "ceiling": r["ceiling"],
                "budget_remaining": r["budget_remaining"], "burn_rate": r["burn_rate"],
                "feasible": r["feasible"],
            })
    mix = defaultdict(int)
    for r in picked: mix[r["agent"]] += 1
    print(f"written  {dst.name}   {len(picked)} rows, {len(HEADER)} columns   {dst.stat().st_size:,} bytes")
    print(f"  agents  " + "  ".join(f"{k} {v}" for k, v in sorted(mix.items())))
    print(f"  worlds  {len({r['seed'] for r in picked})} of 100")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
