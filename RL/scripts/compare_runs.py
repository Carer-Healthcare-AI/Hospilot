"""Put PPO training logs side by side. Used for both the KL-fix check and the world ablation.

Parses the training table by position rather than by regex over the whole line, because the
table has grown columns twice and a fixed pattern silently stops matching instead of failing.
The trailing probe block is 0, 1 or 4 tokens depending on when the log was written; everything
before it is fixed.

Usage:  python scripts/compare_runs.py kldiag_fixed_s0 kldiag_legacy_s0 ...
"""

import statistics
import sys
from pathlib import Path

ART = Path(__file__).resolve().parents[1] / "artifacts"


def parse(label: str):
    path = ART / f"train_ppo.{label}.log"
    if not path.exists():
        return None
    rows, baseline, arm = [], None, "?"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "heuristic scores" in line and "validation" in line:
            baseline = float(line.split("heuristic scores")[1].split()[0])
        if line.startswith("training worlds"):
            arm = "diverse" if "ABLATION B" in line else "fixed"
        t = line.split()
        if len(t) < 19 or not t[0].isdigit() or ":" not in t[-1]:
            continue
        try:
            row = {
                "it": int(t[0]), "steps": int(t[1]), "ep_ret": float(t[2]),
                "entropy": float(t[6]), "kl": float(t[7]),
                "clip": float(t[8].rstrip("%")) / 100.0,
                "ep_p": int(t[10]), "ep_v": int(t[11]),
                "ev": float(t[12]), "corr": float(t[14]),
                "sd_v": float(t[15]), "sd_r": float(t[16]),
                "probe": None, "noaward": None, "wdalt": None, "winnow": None,
            }
        except ValueError:
            continue
        mid = t[18:-1]
        if len(mid) >= 1:
            row["probe"] = float(mid[0])
        if len(mid) >= 4:
            row["noaward"] = float(mid[1].rstrip("%")) / 100.0
            row["wdalt"] = float(mid[2].rstrip("%")) / 100.0
            row["winnow"] = float(mid[3].rstrip("%")) / 100.0
        rows.append(row)
    return {"label": label, "arm": arm, "rows": rows, "baseline": baseline} if rows else None


def slope(pairs):
    """OLS slope of y on x, in units per 1000 env-steps."""
    if len(pairs) < 3:
        return float("nan")
    xs = [p[0] / 1000.0 for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else float("nan")


runs = [r for r in (parse(a) for a in sys.argv[1:]) if r]
if not runs:
    print("no logs matched"); raise SystemExit(1)

print("=" * 108)
print("RUN SHAPE")
print("=" * 108)
print(f"  {'run':<24}{'arm':>9}{'iters':>7}{'steps':>9}{'ep_p':>7}{'ep_v':>7}"
      f"{'heuristic@validation':>22}")
for r in runs:
    rows = r["rows"]
    print(f"  {r['label']:<24}{r['arm']:>9}{len(rows):>7}{rows[-1]['steps']:>9}"
          f"{statistics.fmean(x['ep_p'] for x in rows):>7.2f}"
          f"{statistics.fmean(x['ep_v'] for x in rows):>7.2f}"
          f"{(r['baseline'] or float('nan')):>22.2f}")
print()

print("=" * 108)
print("TRAIN vs VALIDATION — the collapse, if there is one")
print("=" * 108)
print(f"  {'run':<24}{'train first':>12}{'train last':>11}{'val peak':>10}{'val@peak':>9}"
      f"{'val final':>10}{'val slope':>11}{'2nd-half':>10}{'vs heur':>9}")
for r in runs:
    rows = r["rows"]
    curve = [(x["steps"], x["probe"]) for x in rows if x["probe"] is not None]
    if not curve:
        continue
    peak = max(curve, key=lambda c: c[1])
    half = len(curve) // 2
    print(f"  {r['label']:<24}{rows[0]['ep_ret']:>12.1f}{rows[-1]['ep_ret']:>11.1f}"
          f"{peak[1]:>10.1f}{peak[0] // 1000:>8}k{curve[-1][1]:>10.1f}"
          f"{slope(curve):>11.3f}{slope(curve[half:]):>10.3f}"
          f"{curve[-1][1] - (r['baseline'] or 0):>9.1f}")
print()
print("  val slope is return per 1k env-steps over the whole curve; 2nd-half over the last half.")
print("  A collapse is train last > train first WITH a negative val slope.")
print()

print("=" * 108)
print("VALIDATION CURVES")
print("=" * 108)
for r in runs:
    curve = [(x["steps"], x["probe"]) for x in r["rows"] if x["probe"] is not None]
    if not curve:
        continue
    print(f"  {r['label']:<24}({r['arm']}, heuristic {r['baseline']:.1f})")
    print("    " + "  ".join(f"{v:.0f}@{s // 1000}k" for s, v in curve))
print()

print("=" * 108)
print("BEHAVIOUR ON VALIDATION (deterministic serving)")
print("=" * 108)
for r in runs:
    beh = [x for x in r["rows"] if x["noaward"] is not None]
    if not beh:
        print(f"  {r['label']:<24}not recorded in this log")
        continue
    print(f"  {r['label']:<24}({r['arm']})")
    print(f"    {'steps':>8}{'return':>9}{'no-award':>10}{'wdraw_alt':>11}{'win_now':>9}"
          f"{'entropy':>9}{'EV':>8}")
    for x in beh:
        print(f"    {x['steps']:>8}{x['probe']:>9.1f}{x['noaward']:>10.1%}{x['wdalt']:>11.1%}"
              f"{x['winnow']:>9.1%}{x['entropy']:>9.3f}{x['ev']:>+8.3f}")
print()

print("=" * 108)
print("CRITIC AND UPDATE DYNAMICS (mean over last 5 iterations)")
print("=" * 108)
print(f"  {'run':<24}{'EV':>8}{'corr':>8}{'sd(V)':>8}{'sd(R)':>8}{'entropy':>9}"
      f"{'kl':>8}{'clip':>8}")
for r in runs:
    tail = r["rows"][-5:]
    f = statistics.fmean
    print(f"  {r['label']:<24}{f(x['ev'] for x in tail):>+8.3f}"
          f"{f(x['corr'] for x in tail):>+8.3f}{f(x['sd_v'] for x in tail):>8.3f}"
          f"{f(x['sd_r'] for x in tail):>8.3f}{f(x['entropy'] for x in tail):>9.3f}"
          f"{f(x['kl'] for x in tail):>8.4f}{f(x['clip'] for x in tail):>8.1%}")
