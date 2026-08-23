"""Bidding policies. Everything here emits ``alpha``; nothing here emits points.

``Increment = alpha x (Ceiling - CurrentBid)`` (RL-Steps section 6). Constraining the output
to a fraction of remaining headroom is what stops a policy inventing arbitrary values, and it
means the same policy stays structurally valid when the caps are re-fitted.

**Nothing outside this package may know which policy is running.** Every bid row records
``policy_name``, so behaviour data and evaluation data stay separable in the log.

**Guards are not here.** Ceiling, affordability and the safety layer are enforced in
``auction/guards.py``, after the policy has spoken. A constraint enforced inside a policy is a
constraint a learned policy can be trained to violate, whenever violating it once paid off.

Current state::

    heuristic.py   deterministic, reproduces section 18, ships first, stays as the baseline
    rl.py          not built — blocked on the auction log (F-02), the mortality source
                   (F-01) and cap fitting (B.13), in that order
"""

from allocation.policy.heuristic import HeuristicPolicy

__all__ = ["HeuristicPolicy"]
