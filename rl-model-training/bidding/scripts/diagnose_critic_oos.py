"""The in-sample ceiling from ``diagnose_critic_features`` is not a promise. This checks it.

An OLS R-squared fitted and scored on the same 2 400 steps can be inflated by 22 free
parameters over 39 correlated chains. The question that decides whether to touch the encoder
is out-of-sample: fit the value row on half the training worlds, score it on the other half.

* If a held-out linear critic on the CURRENT 22 features reaches ~0.5, the trained critic's
  0.22 is an OPTIMISATION deficit and no feature will fix it.
* If it collapses toward the trained critic's 0.22, the ceiling was overfitting and the
  feature argument is back on the table.

Also priced here: what the oracle remaining-count would buy out-of-sample, which is the
honest upper bound on any legitimate horizon proxy.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from allocation.config import load_config
from allocation.rl.encoder import NAMES, StateEncoder
from allocation.rl.ppo import values
from allocation.rl.ppo_policy import PPOWeights
from allocation.reward.terms import discount_gamma
from allocation.sim.calibrate import _with_base

ART = Path(__file__).resolve().parents[1] / "artifacts"
blob = np.load(ART / "critic_rollout.npz", allow_pickle=True)
X, y, meta = blob["X"], blob["y"], list(blob["meta"])
GAMMA = discount_gamma(_with_base(load_config(), 120.0))

seed_of = np.array([m["seed"] for m in meta])
remaining = np.array([m["remaining"] for m in meta], dtype=float)
disc = (1.0 - GAMMA ** remaining) / (1.0 - GAMMA)

FIT = np.isin(seed_of, (11, 12, 13, 14))
HELD = ~FIT
print(f"fit worlds  seeds 11-14   {FIT.sum()} steps")
print(f"held worlds seeds 15-18   {HELD.sum()} steps")
print()


def ev(pred, actual):
    return 1.0 - float(np.var(actual - pred) / (np.var(actual) + 1e-12))


def fit_score(cols, label):
    """Least-squares on the fit worlds, explained variance on the held-out ones."""
    A = np.column_stack([np.ones(len(y))] + list(cols))
    beta, *_ = np.linalg.lstsq(A[FIT], y[FIT], rcond=None)
    ins, oos = ev(A[FIT] @ beta, y[FIT]), ev(A[HELD] @ beta, y[HELD])
    print(f"  {label:<50}{ins:>+9.3f}{oos:>+11.3f}")
    return oos


ALL = [X[:, i] for i in range(X.shape[1])]

print("=" * 82)
print("HELD-OUT EXPLAINED VARIANCE OF A LINEAR CRITIC")
print("=" * 82)
print(f"  {'critic':<50}{'in-sample':>9}{'held-out':>11}")

weights = PPOWeights.load(ART / "er_policy.ppo_run2_A_s0.final.json", StateEncoder())
V = values(weights, X)
print(f"  {'the TRAINED v_row (no refit)':<50}{ev(V[FIT], y[FIT]):>+9.3f}{ev(V[HELD], y[HELD]):>+11.3f}")

oos_elapsed = fit_score([X[:, NAMES.index("shift_fraction_elapsed")]], "shift_fraction_elapsed alone")
oos_all = fit_score(ALL, "all 22 features (what v_row COULD reach)")
oos_oracle = fit_score(ALL + [remaining, disc], "22 features + ORACLE remaining count")
print()
print(f"  optimisation deficit  (best-linear-22 minus trained)   "
      f"{oos_all - ev(V[HELD], y[HELD]):+.3f}")
print(f"  feature deficit       (oracle minus best-linear-22)    {oos_oracle - oos_all:+.3f}")
print()

print("=" * 82)
print("IS THE HORIZON PROXY ALREADY IN THE VECTOR?")
print("=" * 82)
elapsed = X[:, NAMES.index("shift_fraction_elapsed")]
for rate in (1.8,):
    proxy = rate * (1.0 - elapsed) * 8.0
    A = np.column_stack([np.ones(len(y)), elapsed])
    beta, *_ = np.linalg.lstsq(A, proxy, rcond=None)
    print(f"  expected_auctions_remaining = {rate}/h * hours_left")
    print(f"    R^2 of regressing it on shift_fraction_elapsed alone: "
          f"{ev(A @ beta, proxy):+.6f}")
    print("    A linear critic cannot distinguish a feature from an affine rescaling of a")
    print("    column it already has. R^2 = 1.000000 means the proposed feature is that.")
print()

print("=" * 82)
print("WHAT THE ORACLE HEADROOM ACTUALLY IS")
print("=" * 82)
pred_from_elapsed = np.polyval(np.polyfit(elapsed, remaining, 1), elapsed)
resid = remaining - pred_from_elapsed
print(f"  true remaining count       mean {remaining.mean():5.2f}  sd {remaining.std():5.2f}")
print(f"  predicted from elapsed     sd {pred_from_elapsed.std():5.2f}")
print(f"  residual (unforecastable)  sd {resid.std():5.2f}  "
      f"= {resid.std() / remaining.std():.0%} of the count's own spread")
print("  That residual is the Poisson realisation of a CONSTANT-rate release process. It is")
print("  not a function of anything observable before the releases happen, so no legitimate")
print("  feature can recover it. It is the oracle's whole advantage.")
print()

print("=" * 82)
print("FEATURE CLAMPING CHECK")
print("=" * 82)
for name in ("boarding", "burn_rate", "budget_remaining", "occupancy", "contention"):
    col = X[:, NAMES.index(name)]
    print(f"  {name:<20} at 0.0 {np.mean(col <= 1e-9):>6.1%}   at 1.0 {np.mean(col >= 1 - 1e-9):>6.1%}"
          f"   sd {col.std():.3f}")
