"""Base budget, derived from target win counts. AGENT_BUDGET sections 3-4.

The circularity that blocks calibration is budget -> bids -> spend -> budget. It breaks once
you notice what the budget *is*: a department with budget ``B`` and cost-per-win ``c`` can win
about ``B/c`` auctions per shift. **The budget is a target win count wearing point-shaped
clothing.**

So the derivation runs in the direction that has an answerable question at the end::

    x  "How many points should ER get?"                    nobody can answer this
    v  "Of ER's ~6 ICU requests this shift, how many
        should ER expect to win?"                          a medical director can answer this

    E[bid]        = E[utility] x E[alpha]
    cost_per_win  = E[bid] x contention x 1.0 x commitment_rate
    cost_per_loss = E[bid] x contention x 0.1 x commitment_rate
    Base          = N_win x cost_per_win + (N_req - N_win) x cost_per_loss

**``N_win`` is the real governance artefact.** It is where departmental priority is actually
set, and it should be signed off explicitly rather than emerging from a product of multipliers
nobody can trace. It is currently assumed (BA1) and :meth:`Config.unsigned` says so.

Two consequences to accept, both from AGENT_BUDGET 4.1:

* **The budget is denominated in utility points, so it moves when the caps move.** Re-fit the
  caps (B.13) and every budget must be re-derived. Store ``caps_version`` on every row.
* Base changes only when caps, ``N_win`` or ``N_req`` change — not per shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from allocation.config import Config
from allocation.contracts import AgentKind


@dataclass(frozen=True, slots=True)
class BaseBudget:
    """A department's Base, with every input retained so it can be re-derived."""

    agent: AgentKind
    expected_utility: float
    expected_alpha: float
    expected_bid: float
    contention: float
    commitment_rate: float
    cost_per_win: float
    cost_per_loss: float
    n_win: int
    n_req: int
    base: float
    caps_version: str
    #: ``common`` (RL-Steps section 4) or ``derived`` (AGENT_BUDGET v0.3).
    source: str = "derived"

    @property
    def theoretical_burn(self) -> float:
        """Burn if the department hits its targets exactly.

        Under ``derived`` this is 1.0 by construction — Base is *defined* as that spend, so
        the number proves nothing beyond the arithmetic being right.

        Under ``common`` it is a real measurement, and the one that says whether the shared
        Base is the right size. AGENT_BUDGET section 8's test: *"A department that bids its
        ceiling on every case must run out before the shift ends."* Far below 1.0 means the
        budget never binds and bidding maximum is free.
        """
        if self.base <= 0:
            return 0.0
        spend = self.n_win * self.cost_per_win + (self.n_req - self.n_win) * self.cost_per_loss
        return spend / self.base


def derive_base(config: Config, agent: AgentKind) -> BaseBudget:
    """One department's Base. Two modes, selected by the resource's budget pool, ``base.mode``.

    ``common`` is RL-Steps section 4: an identical Base for every department, with Demand,
    Criticality, Scarcity and Fairness carrying all the difference between them.

    ``derived`` is AGENT_BUDGET v0.3: Base from target win counts, per department.
    """
    mode = str(config.budget.get("base", {}).get("mode", "derived")).lower()
    if mode == "common":
        return _common_base(config, agent)
    if mode == "derived":
        return _derived_base(config, agent)
    raise ValueError(
        f"budget pool base.mode is {mode!r}; expected 'common' (RL-Steps section 4) "
        "or 'derived' (AGENT_BUDGET v0.3)"
    )


def _common_base(config: Config, agent: AgentKind) -> BaseBudget:
    """RL-Steps section 4's single shared Base.

    The target counts are still read and still recorded, because ``theoretical_burn`` is the
    check that says whether the chosen number is the right size. Under a common Base it is no
    longer 1.0 by construction, and that is the point: it becomes a diagnostic rather than a
    tautology.
    """
    cfg = config.budget["base"]
    base = float(cfg["common_points"])
    if base <= 0:
        raise ValueError("base.common_points must be positive")

    spend_cfg = config.budget["spend"]
    derivation = config.budget["base_derivation"]
    targets = config.budget["targets"].get(agent.value, {})

    utility = float(derivation["expected_utility"].get(agent.value, 0.0))
    alpha = float(derivation["expected_alpha"])
    contention = float(derivation["reference_contention"])
    rate = float(spend_cfg["commitment_rate"])

    expected_bid = utility * alpha
    return BaseBudget(
        agent=agent,
        expected_utility=utility,
        expected_alpha=alpha,
        expected_bid=expected_bid,
        contention=contention,
        commitment_rate=rate,
        cost_per_win=expected_bid * contention * float(spend_cfg["outcome_factor"]["win"]) * rate,
        cost_per_loss=expected_bid * contention * float(spend_cfg["outcome_factor"]["lose"]) * rate,
        n_win=int(targets.get("n_win", 0)),
        n_req=int(targets.get("n_req", 0)),
        base=base,
        caps_version=config.caps_version,
        source="common",
    )


def _derived_base(config: Config, agent: AgentKind) -> BaseBudget:
    """AGENT_BUDGET v0.3. Raises if the targets are incoherent."""
    budget_cfg = config.budget
    derivation = budget_cfg["base_derivation"]
    spend_cfg = budget_cfg["spend"]

    try:
        utility = float(derivation["expected_utility"][agent.value])
        targets = budget_cfg["targets"][agent.value]
    except KeyError as exc:
        raise KeyError(
            f"the budget pool for {budget_cfg.get('resource_type', 'this resource')} has no "
            f"entry for agent {agent.value!r}. Under base.mode 'derived' every eligible "
            "bidder needs an expected_utility and a targets row; 'icu' has neither, and it "
            "became an eligible bidder for step-down beds in Step 10."
        ) from exc

    n_win = int(targets["n_win"])
    n_req = int(targets["n_req"])
    if n_win > n_req:
        raise ValueError(
            f"{agent.value}: target wins ({n_win}) exceeds expected requests ({n_req})"
        )
    if n_win < 0 or n_req < 0:
        raise ValueError(f"{agent.value}: target counts must be non-negative")

    alpha = float(derivation["expected_alpha"])
    contention = float(derivation["reference_contention"])
    rate = float(spend_cfg["commitment_rate"])
    win_factor = float(spend_cfg["outcome_factor"]["win"])
    loss_factor = float(spend_cfg["outcome_factor"]["lose"])

    expected_bid = utility * alpha
    cost_per_win = expected_bid * contention * win_factor * rate
    cost_per_loss = expected_bid * contention * loss_factor * rate
    base = n_win * cost_per_win + (n_req - n_win) * cost_per_loss

    return BaseBudget(
        agent=agent,
        expected_utility=utility,
        expected_alpha=alpha,
        expected_bid=expected_bid,
        contention=contention,
        commitment_rate=rate,
        cost_per_win=cost_per_win,
        cost_per_loss=cost_per_loss,
        n_win=n_win,
        n_req=n_req,
        base=base,
        caps_version=config.caps_version,
    )


def derive_all(config: Config, agents: tuple[AgentKind, ...]) -> Mapping[AgentKind, BaseBudget]:
    return {agent: derive_base(config, agent) for agent in agents}
