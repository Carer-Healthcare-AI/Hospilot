"""Constraints applied **after** the policy has spoken.

The policy proposes; the auction disposes. Three reasons this lives here and not inside
``policy/``:

* A constraint enforced inside a policy is one a *learned* policy can be trained to violate,
  whenever violating it once paid off in the log. Enforcing it outside makes that impossible
  rather than unlikely.
* The heuristic and a future network must be subject to identical limits, or their logged
  episodes are not comparable and the RL cannot be evaluated against its baseline.
* Every clamp is recorded with a reason, so a bid that was cut can be told apart from a bid
  that was chosen — which matters enormously when fitting anything to this log.

Three guards today, and one of them is empty:

``ceiling``       ``Bid <= Ceiling``. Never exceed clinical value (section 6).
``affordability`` ``Cost <= Remaining``, i.e. ``Bid <= Remaining / (Contention x Outcome x
                  Rate)``. Not ``Bid <= Remaining``, which over-restricts by 4x at rate 0.25.
``safety``        **UNDECLARED.** END_TO_END section 1 marks the safety layer 🟥 "unspecified
                  in RL-Steps". ``auction.yaml`` carries an empty constraint list, so nothing
                  is currently enforced — and that is a live gap (F-13), not a passing state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from allocation.budget.spend import max_affordable_bid
from allocation.config import Config
from allocation.contracts import Candidate


@dataclass(frozen=True, slots=True)
class GuardedBid:
    """A bid after clamping, with the reason recorded if it moved."""

    amount: float
    proposed: float
    clamped_by: str = ""

    @property
    def was_clamped(self) -> bool:
        return bool(self.clamped_by)


def apply_guards(
    config: Config,
    proposed: float,
    ceiling: float,
    remaining_budget: float,
    contention: float,
    quantise: bool = True,
) -> GuardedBid:
    """Clamp a proposed bid to the ceiling and to affordability, tightest first.

    **Quantisation happens here, after the clamps, not in the caller.** Bids are whole points
    in the worked example, and rounding a clamped amount *outside* this function silently
    breaks the guard: a ceiling of 107.6 clamps a bid to 107.6, which ``round`` then lifts to
    108 — above the ceiling the clamp had just enforced. Ceilings are fractional almost
    always (107.4, 111.9, 156.7), so this is the common case rather than an edge one.

    So: round to nearest, and if that crosses a binding limit, floor instead. Section 17's
    118.5 still resolves to 118 against a ceiling of 171, because the limit is not binding
    there — the rule only bites when the bid is already at its maximum.
    """
    amount = proposed
    reason = ""

    if amount > ceiling:
        amount = ceiling
        reason = "ceiling"

    affordable = max_affordable_bid(config, remaining_budget, contention, won=True)
    if amount > affordable:
        amount = affordable
        reason = "affordability" if not reason else f"{reason}+affordability"

    amount = max(0.0, amount)

    if quantise:
        limit = max(0.0, min(ceiling, affordable))
        rounded = float(round(amount))
        amount = float(math.floor(limit)) if rounded > limit else rounded

    return GuardedBid(amount=amount, proposed=proposed, clamped_by=reason)


#: Rule ids this build knows how to enforce, and where each is evaluated.
#:
#: A constraint listed in ``auction.yaml`` whose ``rule`` is not in here is REFUSED at read
#: time. That is the property the original ``NotImplementedError`` protected and it is kept
#: exactly: a rule that is written down but not enforced is worse than one that was never
#: written, because it reads like protection. Adding a rule to the YAML therefore requires
#: adding an evaluator here or in ``rl/pilot.py`` — there is no path where a listed rule is
#: silently ignored.
KNOWN_SAFETY_RULES: dict[str, str] = {
    "never_abandon_when_planned_exit_available": "policy_gate",
    "never_abandon_at_or_above_news2": "policy_gate",
    "bid_within_ceiling": "auction_guard",
}

UNDECLARED = "undeclared"
PROVISIONAL = "provisional"
SIGNED_OFF = "signed_off"


def safety_rules(config: Config) -> tuple[dict, ...]:
    """The declared constraints, refusing any this build cannot enforce."""
    constraints = tuple(config.auction.get("safety_constraints") or ())
    unknown = sorted(
        {str(c.get("rule", c.get("id", "?"))) for c in constraints}
        - set(KNOWN_SAFETY_RULES)
    )
    if unknown:
        raise NotImplementedError(
            f"auction.yaml declares safety rules with no evaluator: {unknown}. Refusing to run "
            "under constraints that are not enforced — add an evaluator (guards.py for "
            "candidate/bid rules, rl/pilot.py for decision rules) and register the id in "
            f"KNOWN_SAFETY_RULES. Known: {sorted(KNOWN_SAFETY_RULES)}"
        )
    return constraints


def safety_rule(config: Config, rule: str) -> dict | None:
    """One declared rule by id, or ``None`` when it is not in force."""
    for constraint in safety_rules(config):
        if str(constraint.get("rule", constraint.get("id"))) == rule:
            return constraint
    return None


def safety_violations(config: Config, candidate: Candidate) -> tuple[str, ...]:
    """Hard clinical constraints evaluated against a candidate, before any bid.

    Returns empty today because all three declared rules are decision-level or bid-level:
    ``bid_within_ceiling`` is enforced in :func:`clamp`, and the two abandonment rules need the
    proposed action, so they live in ``rl/pilot.py``'s ``SafetyGate``. The call still validates
    the declared set, so an unenforceable rule fails here rather than passing quietly.

    This function exists so that the absence is visible at the call site rather than being an
    omission nobody notices — an empty rule set is a decision that was deferred, and the policy
    will exploit any rule left unwritten.
    """
    safety_rules(config)
    return ()


def safety_posture(config: Config) -> str:
    """``undeclared`` | ``provisional`` | ``signed_off``."""
    status = str(config.auction.get("safety_status", "")) or UNDECLARED
    if status not in (UNDECLARED, PROVISIONAL, SIGNED_OFF):
        raise ValueError(
            f"auction.yaml safety_status is {status!r}; expected one of "
            f"{UNDECLARED!r}, {PROVISIONAL!r}, {SIGNED_OFF!r}"
        )
    return status


def safety_is_declared(config: Config) -> bool:
    """True only for a real clinical sign-off. ``provisional`` is deliberately not enough.

    Kept strict because callers use it to answer *"has a clinician approved this?"* — the API
    reports it as ``safety_constraints_declared``. Whether a learned policy may act is a
    different and weaker question; use :func:`safety_is_enforced` for that.
    """
    return safety_posture(config) == SIGNED_OFF


def safety_is_enforced(config: Config) -> bool:
    """True when rules are declared AND every one of them has an evaluator.

    The gate for letting a learned policy act. ``provisional`` passes: the constraints bind
    even though no clinician has approved their content, which is the difference between a
    supervised pilot and an unsupervised one.
    """
    if safety_posture(config) == UNDECLARED:
        return False
    return bool(safety_rules(config))
