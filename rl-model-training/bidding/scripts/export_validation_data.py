"""Materialise the validation sets to REAL FILES on disk.

Run:  python scripts/export_validation_data.py [--quick]

Until this runs, the validation sets exist only as a seed range in a config. That is
reproducible but not showable: you cannot hand somebody a path and say "this is what the model
was tested on". This script writes the worlds out so you can.

Three sets, each in two formats:

  validation.ppo.seeds205-300      the band every PPO ablation and sweep number uses
  validation.gate.seeds101-200     the band every gate verdict uses (Q, CEM and PPO)
  holdout.q_offline                the 25% the offline-Q fit never trained on

  .jsonl  full fidelity, one row per decision, same schema as transitions.jsonl
  .csv    flat, one row per decision, 22 state features as named columns -- opens in Excel

EVERY ROW CARRIES ITS OWN seed AND world COLUMN. ``Transition.as_dict`` does not emit the seed
(dataset.py:100-122), which is the reason the seed bands were only ever visible in code. Here
they are a column, so the file is self-describing and nobody has to be told how the seeds work.

The row COUNT is the evidence: these are separate decisions, in separate worlds, that no
learner saw during training. Cross-check any seed against artifacts/transitions.jsonl and it is
absent -- the training corpus is seeds 7000-7039 and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import NAMES, StateEncoder
from allocation.rl.qlearn import load_transitions
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
BASE = 120.0

#: The flat CSV header. Scalars first, then the 22 state features by name, then the chain.
SCALARS = [
    "seed", "world", "shift_id", "auction_id", "agent", "candidate_id",
    "q_action", "alpha", "won", "bid", "utility", "ceiling", "cost", "reward",
    "budget_remaining", "burn_rate", "terminal", "complete", "feasible",
]


def _flat(row: dict) -> dict:
    """One transition as a flat CSV record, state exploded into named columns."""
    out = {k: row.get(k) for k in SCALARS}
    out["feasible"] = "|".join(row.get("feasible") or [])
    for name, value in zip(NAMES, row["state"]):
        out[f"s_{name}"] = f"{value:.6f}"
    nxt = row.get("next_state")
    for name in NAMES:
        out[f"n_{name}"] = ""
    if nxt:
        for name, value in zip(NAMES, nxt):
            out[f"n_{name}"] = f"{value:.6f}"
    return out


CSV_HEADER = SCALARS + [f"s_{n}" for n in NAMES] + [f"n_{n}" for n in NAMES]


def _write(stem: str, rows: list[dict], header: dict) -> tuple[Path, Path]:
    """Write one set as .jsonl (full fidelity) and .csv (flat, Excel-openable)."""
    jsonl = OUT / f"{stem}.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(header) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    csv_path = OUT / f"{stem}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_flat(row))

    print(f"  {jsonl.name:<38} {len(rows):>6} rows  {jsonl.stat().st_size:>11,} bytes")
    print(f"  {csv_path.name:<38} {len(rows):>6} rows  {csv_path.stat().st_size:>11,} bytes")
    return jsonl, csv_path


def _collect(config, fab, encoder, seeds, shifts, label):
    """Generate every world in a band and return its rows, seed-stamped."""
    rows: list[dict] = []
    per_seed: dict[int, int] = {}
    returns: list[float] = []
    auctions = abandonments = episodes = 0
    print(f"\n{label}: generating {len(seeds)} worlds x {shifts} shifts ...")
    for i, seed in enumerate(seeds):
        d = generate(config, seed=seed, shifts=shifts, fab=fab, encoder=encoder)
        auctions += d.auctions
        abandonments += d.abandonments
        episodes += len(d.episodes)
        returns += [
            e.discounted_return for e in d.complete_episodes if e.agent is AgentKind.ER
        ]
        for t in d.transitions:
            row = dict(t.as_dict())
            # as_dict has no seed. Without it the file cannot say which world it came from,
            # which is exactly the gap these exports exist to close.
            row["seed"] = seed
            row["world"] = f"seed-{seed}"
            rows.append(row)
        per_seed[seed] = len(d.transitions)
        if (i + 1) % 20 == 0:
            print(f"    {i + 1:>3}/{len(seeds)} worlds   {len(rows):>6} rows", flush=True)
    stats = {
        "rows": len(rows),
        "auctions": auctions,
        "abandonments": abandonments,
        "episodes": episodes,
        "er_return_mean": round(statistics.fmean(returns), 2) if returns else 0.0,
        "er_return_sd": round(statistics.pstdev(returns), 2) if returns else 0.0,
        "rows_per_seed_min": min(per_seed.values()),
        "rows_per_seed_max": max(per_seed.values()),
    }
    return rows, stats


def _preview(stem: str, rows: list[dict], title: str, blurb: list[str]) -> Path:
    """A short human-readable file: what this set is, then three decisions in full."""
    lines = ["=" * 92, title, "=" * 92, ""]
    lines += blurb
    lines += ["", "-" * 92, "THREE REAL ROWS, IN FULL", "-" * 92, ""]
    for t in rows[:3]:
        lines.append(f"  world {t['world']}   shift {t['shift_id']}   auction {t['auction_id']}")
        lines.append(f"    agent {t['agent']}   candidate {t['candidate_id']}")
        lines.append(f"    DECISION  action={t['q_action']}  alpha={t['alpha']}")
        lines.append(f"    OUTCOME   won={t['won']}  bid={t['bid']:.1f}  cost={t['cost']:.1f}  "
                     f"reward={t['reward']:.1f}")
        lines.append(f"    CONTEXT   utility={t['utility']:.1f}  ceiling={t['ceiling']:.1f}  "
                     f"budget_left={t['budget_remaining']:.1f}  burn={t['burn_rate']:.3f}")
        lines.append(f"    feasible  {t['feasible']}")
        lines.append(f"    terminal  {t['terminal']}"
                     f"   (next_state {'present' if t.get('next_state') else 'None - shift ended'})")
        lines.append("    STATE the model saw, 22 features:")
        for name, value in zip(NAMES, t["state"]):
            lines.append(f"      s_{name:<24} {value:.6f}")
        lines.append("")
    path = OUT / f"{stem}.preview.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {path.name:<38} human-readable, 3 rows in full")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="10 seeds per band instead of the full range, for a smoke test")
    args = parser.parse_args(argv)

    config = _with_base(load_config(), BASE)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    encoder = StateEncoder()
    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    manifest = {"written": stamp, "sets": {}}

    n = 10 if args.quick else None

    # ---- 1. PPO validation band -------------------------------------------------------
    ppo_seeds = list(range(205, 301))[:n] if n else list(range(205, 301))
    rows, stats = _collect(config, fab, encoder, ppo_seeds, 4, "PPO VALIDATION 205-300")
    header = {
        "_header": True, "set": "ppo_validation",
        "seeds": f"{ppo_seeds[0]}-{ppo_seeds[-1]}", "n_seeds": len(ppo_seeds), "shifts": 4,
        "encoder_version": encoder.version, "fabrication_version": fab.version,
        "purpose": "PPO checkpoint selection and the validation curve. Disjoint from PPO "
                   "training seeds 11-18 and from the 101-200 gate.",
        "heuristic_return_here": 635.83, "written": stamp, **stats,
    }
    manifest["sets"]["ppo_validation"] = header
    _write("validation.ppo.seeds205-300", rows, header)
    _preview("validation.ppo.seeds205-300", rows, "PPO VALIDATION DATA - seeds 205-300", [
        "  These are decisions from 96 simulated hospital worlds that PPO never trained on.",
        "  PPO trained on worlds 11-18 (arm A) or 10,000-100,000 (arm B). Neither overlaps",
        "  this range, so every row here is a situation the model is seeing for the first time.",
        "",
        f"  worlds        {len(ppo_seeds)}  (seeds {ppo_seeds[0]}-{ppo_seeds[-1]}, 4 shifts each)",
        f"  decisions     {stats['rows']}",
        f"  auctions      {stats['auctions']}",
        f"  episodes      {stats['episodes']}",
        f"  abandonments  {stats['abandonments']}",
        "",
        "  The heuristic baseline scores 635.83 on this set. PPO's best checkpoint scored",
        "  695.4 (+9.4%) and then decayed to 533.3 by the end of training.",
        "",
        "  Each row is ONE department's decision in ONE auction: the 22-number state it saw,",
        "  the action it chose, and what that earned. The model reads the state and outputs",
        "  the action; everything else is the outcome the simulator returned.",
    ])

    # ---- 2. Gate band -----------------------------------------------------------------
    gate_seeds = list(range(101, 201))[:n] if n else list(range(101, 201))
    rows, stats = _collect(config, fab, encoder, gate_seeds, 6, "GATE 101-200")
    header = {
        "_header": True, "set": "gate",
        "seeds": f"{gate_seeds[0]}-{gate_seeds[-1]}", "n_seeds": len(gate_seeds), "shifts": 6,
        "encoder_version": encoder.version, "fabrication_version": fab.version,
        "purpose": "The pre-registered gate. Every scorecard verdict for Q, CEM and PPO is "
                   "measured here. Disjoint from all training bands.",
        "heuristic_return_here": 713.93, "written": stamp, **stats,
    }
    manifest["sets"]["gate"] = header
    _write("validation.gate.seeds101-200", rows, header)
    _preview("validation.gate.seeds101-200", rows, "GATE VALIDATION DATA - seeds 101-200", [
        "  These are decisions from 100 simulated hospital worlds used for the FINAL verdict",
        "  on every policy in this project - Q-learning, CEM and PPO alike.",
        "",
        "  No learner trained on these worlds:",
        "    offline Q trained on   seeds 7000-7039",
        "    online Q collected on  seeds 1000-2069",
        "    PPO trained on         seeds 11-18",
        "    CEM fitted on          seeds 11-18",
        "  None of those ranges touches 101-200.",
        "",
        f"  worlds        {len(gate_seeds)}  (seeds {gate_seeds[0]}-{gate_seeds[-1]}, 6 shifts each)",
        f"  decisions     {stats['rows']}",
        f"  auctions      {stats['auctions']}",
        f"  episodes      {stats['episodes']}",
        f"  abandonments  {stats['abandonments']}",
        "",
        "  The heuristic baseline scores 713.93 here. Measured against it:",
        "    offline Q   217.07   -69.6%   t=-26.74",
        "    online Q    648.84    -9.1%   t=-4.55",
        "    PPO run 1   378.47   -47.0%   t=-20.49",
        "",
        "  Scoring is PAIRED: both policies run the same 689 shifts and the difference is",
        "  taken shift by shift. That matters because returns swing from 20 to 1546 between",
        "  shifts, and an unpaired average would drown a real 40-point effect in that spread.",
    ])

    # ---- 3. Offline-Q holdout ---------------------------------------------------------
    data = OUT / "transitions.jsonl"
    if data.exists():
        print("\nOFFLINE-Q HOLDOUT: reproducing the fit-time split ...")
        trans, src = load_transitions(str(data), agent=AgentKind.ER)
        usable = [t for t in trans if t.complete]
        rng = random.Random(0)
        keys = sorted({(t.agent.value, t.shift_id) for t in usable})
        rng.shuffle(keys)
        cut = int(len(keys) * 0.75)
        train_keys = set(keys[:cut])
        hold = [t for t in usable if (t.agent.value, t.shift_id) not in train_keys]
        rows = []
        for t in hold:
            row = dict(t.as_dict())
            # The corpus header records one seed for the whole file; per-row provenance was
            # never written, so it cannot be recovered here. Say so rather than guess.
            row["seed"] = None
            row["world"] = "7000-7039 (pooled; per-row seed not recorded in the corpus)"
            rows.append(row)
        header = {
            "_header": True, "set": "q_offline_holdout",
            "source_corpus": data.name, "split": "25% by (agent, shift_id), random.Random(0)",
            "encoder_version": encoder.version,
            "fabrication_version": src.get("fabrication_version"),
            "purpose": "The rows the offline-Q TD fit never trained on.",
            "caveat": "shift_id carries no seed (budget/shifts.py:86), so this split holds out "
                      "CALENDAR SLOTS, not worlds. All 40 corpus seeds appear on both sides. "
                      "It is a valid chain-preserving split and NOT evidence about new worlds.",
            "written": stamp, "rows": len(rows),
            "distinct_keys_total": len(keys), "keys_train": cut, "keys_holdout": len(keys) - cut,
        }
        manifest["sets"]["q_offline_holdout"] = header
        _write("holdout.q_offline", rows, header)
        _preview("holdout.q_offline", rows, "OFFLINE-Q HOLDOUT - rows the TD fit never saw", [
            "  These rows come out of the training corpus itself. The fit shuffles the shift",
            "  keys with a fixed seed and keeps 25% back, so the learner is scored on decisions",
            "  it never updated on.",
            "",
            f"  rows held out    {len(rows)} of {len(usable)}",
            f"  shift keys       {len(keys)} total -> {cut} train / {len(keys) - cut} holdout",
            "",
            "  READ THIS BEFORE CITING IT. shift_id is the date plus the slot label and carries",
            "  no seed, so all 40 corpus worlds land in the same handful of keys and every one",
            "  of them appears on BOTH sides of the split. This holds out times of day, not",
            "  worlds. It is a fair test of 'did the value function memorise these rows' and it",
            "  is NOT a test of 'does it work in a world it has never seen'.",
            "",
            "  For the second question use validation.gate.seeds101-200, where the worlds",
            "  genuinely are new.",
        ])

    (OUT / "validation_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n  validation_manifest.json               index of all sets")
    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
