"""Running a learned policy safely: shadow first, gated always, never straight to live.

**Nothing trained in ``sim/`` is fit to allocate a bed.** RL_READINESS §2.B is the governing
sentence and it does not soften with a good sweep result: a simulator comparison answers which
policy paces better, never which policy saves more patients. So the path to production is not
"train, evaluate, deploy" — it is "train, evaluate, **shadow against reality for months**, and
only then discuss whether anything should change".

Three mechanisms, in the order they should be used.

**1 · Shadow.** :class:`ShadowPolicy` runs both policies on every decision, *acts on the
heuristic*, and records what the learned one would have done. Every bed is allocated by the
rule-based policy that ships today; the learned policy accumulates a track record against real
auctions with no patient exposed to it. ``AuctionMode.ADVISORY`` is the matching audit mode —
already in the enum, already meaning "scored and logged identically, holds no bed".

**2 · Gates.** :class:`SafetyGate` refuses specific learned decisions regardless of their
Q-value. Gates are not a fallback for a bad policy; they are the constraints that must hold even
for a good one. ``config/auction.yaml``'s ``safety_constraints`` is **currently an empty list
marked `undeclared`** — the loudest unsigned item in the whole system, and the one that must be
filled before a pilot rather than after it. The gates below are this module's proposal for what
belongs there; they are ours, and they are marked as such.

**3 · Circuit breaker.** :class:`DivergenceMonitor` halts the pilot when the learned policy
disagrees with the baseline more than a stated fraction of the time. A policy that diverges on
most decisions has either found something real or drifted onto out-of-distribution states, and
the shadow log cannot tell which — so the safe reading is the second.

**Why gates live here and not inside the policy.** ``policy/__init__.py``: *"A constraint
enforced inside a policy is a constraint a learned policy can be trained to violate, whenever
violating it once paid off."* A gate the optimiser can see is a gate the optimiser will route
around. These wrap the policy from outside, after it has spoken, exactly as
``auction/guards.py`` does for the bid arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    BiddingPolicy,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    QAction,
    RoundState,
    UtilityBreakdown,
)
from allocation.auction.guards import safety_rule
from allocation.pathway.plans import build_plan


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Whether a decision was allowed, and what replaced it if not."""

    allowed: bool
    rule: str = ""
    substituted: Decision | None = None


@dataclass(frozen=True, slots=True)
class SafetyGate:
    """Hard constraints a learned decision must satisfy, checked after the policy speaks.

    Every rule here is **ours**, not signed, and belongs in ``auction.yaml``'s
    ``safety_constraints`` once somebody with clinical standing has written it down. They are
    proposed rather than asserted, and the empty ``safety_constraints`` list is the reason this
    class exists in code rather than as config: an empty list enforces nothing, and a pilot
    behind no constraints at all is not a pilot.
    """

    config: Config

    #: A patient whose NEWS2 is at or above this must never be abandoned. Abandonment is the
    #: one action with no onward plan, and applying it to a critically ill patient is the
    #: failure mode with no recovery path — every other exit at least arranges something.
    #:
    #: ``None`` means *read it from* ``auction.yaml``'s ``never_abandon_at_or_above_news2``
    #: rule, which is the path every caller should take. A literal here overrides config and
    #: exists for tests; the previous hard-coded 7.0 was a threshold living in code where no
    #: clinical review would ever find it.
    critical_news2: float | None = None

    #: A learned policy must never bid above the utility ceiling. Already enforced in
    #: ``auction/guards.py``; repeated here so a gate audit reads as a complete list rather
    #: than requiring a reader to know which constraints live where.
    enforce_ceiling: bool = True

    @property
    def news2_limit(self) -> float | None:
        """The configured threshold, or ``None`` when the rule is not in force."""
        if self.critical_news2 is not None:
            return float(self.critical_news2)
        rule = safety_rule(self.config, "never_abandon_at_or_above_news2")
        return None if rule is None else float(rule["threshold"])

    @property
    def forbids_avoidable_abandonment(self) -> bool:
        return safety_rule(self.config, "never_abandon_when_planned_exit_available") is not None

    def check(
        self,
        decision: Decision,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        pathways: PathwayOptions | None,
        news2: float | None = None,
    ) -> GateVerdict:
        """Allow, or substitute the safest available alternative.

        Substitution rather than rejection: an auction round needs *a* decision, and refusing
        one would leave the agent in an undefined state. The substitute is always the most
        conservative action still available — keep competing if the agent can, and only then
        fall back.

        Both abandonment rules are read from ``auction.yaml``. A rule absent from the config is
        not enforced, and ``guards.safety_rules`` refuses at read time if the config names a
        rule this build cannot evaluate — so the set that binds is exactly the set declared.
        """
        if decision.q_action is not QAction.WITHDRAW_UNPLANNED:
            return GateVerdict(allowed=True)

        # Rule: never abandon when something could have been arranged. No threshold, so no
        # clinical judgement is embedded in it — if a planned exit was feasible, the unplanned
        # one is refused. This is the defect the first trained policy exhibited six times.
        if self.forbids_avoidable_abandonment:
            planned = sorted(
                a.value for a in decision.feasible
                if a.exits and a is not QAction.WITHDRAW_UNPLANNED
            )
            if planned:
                return GateVerdict(
                    allowed=False,
                    rule=(
                        "an unplanned withdrawal was chosen while a planned exit was "
                        f"available ({', '.join(planned)})"
                    ),
                    substituted=self._planned_exit(decision, pathways),
                )

        limit = self.news2_limit
        if limit is not None and news2 is not None and news2 >= limit:
            rule = (
                f"NEWS2 {news2:g} >= {limit:g}: a critically ill patient may not be abandoned"
            )
            return GateVerdict(allowed=False, rule=rule, substituted=self._safest(pathways))

        return GateVerdict(allowed=True)

    @staticmethod
    def _planned_exit(decision: Decision, pathways: PathwayOptions | None) -> Decision:
        """Swap an unplanned withdrawal for a planned one that arranges something.

        Preference order is by how much each arranges: a bed in another unit beats a predicted
        release, which beats a re-entry. Falls back to competing if no plan can actually be
        built — a plan that cannot be named would fail to construct, and refusing an exit into
        another impossible exit would leave the round with no decision.
        """
        for action in (
            QAction.WITHDRAW_ALTERNATIVE,
            QAction.AWAIT_NEXT_RESOURCE,
            QAction.RE_ENTER_LATER,
        ):
            if action not in decision.feasible:
                continue
            plan = build_plan(action, pathways, note="safety gate: planned exit substituted")
            if plan is not None:
                return Decision(
                    q_action=action,
                    action=Action.WITHDRAW,
                    plan=plan,
                    feasible=decision.feasible,
                )
        return SafetyGate._safest(pathways)

    @staticmethod
    def _safest(pathways: PathwayOptions | None) -> Decision:
        """Keep competing. The conservative choice when an exit has been refused.

        Continuing costs budget and may still lose, but it is the only action that leaves the
        patient in contention for the bed. An exit substituted for another exit would still be
        an exit.
        """
        return Decision.compete(QAction.CONTINUE, Action.INCREASE_BID, 0.25)


@dataclass
class DivergenceMonitor:
    """Tracks how often the learned policy disagrees with the baseline, and trips.

    The threshold is deliberately not a quality bar. High divergence is not evidence the policy
    is wrong — it may be evidence it is right. It is evidence that the shadow log cannot tell
    the difference, because a policy acting far from the baseline is being evaluated on states
    the baseline never visits and the comparison stops being paired. Tripping means *stop and
    look*, not *the policy failed*.
    """

    threshold: float = 0.35
    window: int = 200
    _recent: list[bool] = field(default_factory=list)
    _total: int = 0
    _diverged: int = 0
    _by_action: dict[str, int] = field(default_factory=dict)

    def record(self, baseline: Decision, learned: Decision) -> None:
        differs = baseline.q_action is not learned.q_action
        self._total += 1
        self._diverged += int(differs)
        self._recent.append(differs)
        if len(self._recent) > self.window:
            self._recent.pop(0)
        if differs:
            key = f"{baseline.q_action.value} -> {learned.q_action.value}"
            self._by_action[key] = self._by_action.get(key, 0) + 1

    @property
    def rate(self) -> float:
        return self._diverged / self._total if self._total else 0.0

    @property
    def recent_rate(self) -> float:
        """Divergence over the trailing window — what the breaker actually reads.

        A cumulative rate is dominated by history and would not notice a policy that started
        diverging today, which is the case the breaker exists for.
        """
        return sum(self._recent) / len(self._recent) if self._recent else 0.0

    @property
    def tripped(self) -> bool:
        return len(self._recent) >= min(self.window, 30) and self.recent_rate > self.threshold

    @property
    def observed(self) -> int:
        """Decisions recorded. Public because a caller rendering this as data rather than as
        :meth:`report` text needs the denominator behind :attr:`rate` — a divergence of 40 %
        over five decisions and over five hundred are not the same claim."""
        return self._total

    @property
    def disagreements(self) -> Mapping[str, int]:
        """``{"continue -> win_now": 110}``, commonest first. The same content
        :meth:`report` tabulates, for a caller that is not printing it."""
        return dict(sorted(self._by_action.items(), key=lambda kv: -kv[1]))

    def report(self) -> str:
        lines = [
            f"decisions observed   {self._total}",
            f"divergence           {self.rate:.1%} overall, {self.recent_rate:.1%} recent",
            f"breaker              {'TRIPPED' if self.tripped else 'ok'} "
            f"(threshold {self.threshold:.0%})",
        ]
        if self._by_action:
            lines += ["", "  most common disagreements"]
            lines += [
                f"    {k:<48} {v:>5}"
                for k, v in sorted(self._by_action.items(), key=lambda kv: -kv[1])[:8]
            ]
        return "\n".join(lines)


class ShadowPolicy:
    """Acts on the baseline, records what the learned policy would have done.

    **The bed is always allocated by the policy that ships today.** This is the mechanism that
    makes a pilot safe rather than merely careful: there is no configuration of this class in
    which the learned policy's decision reaches an auction, so no patient is exposed to it while
    it accumulates a track record.

    What comes out is a paired log — same state, two decisions — which is the only kind of
    evidence that can support a later argument for promotion. An unpaired log of a learned
    policy running alone answers nothing, because the states it visits are its own.
    """

    def __init__(
        self,
        baseline: BiddingPolicy,
        learned: BiddingPolicy,
        gate: SafetyGate | None = None,
        monitor: DivergenceMonitor | None = None,
    ) -> None:
        self._baseline = baseline
        self._learned = learned
        self._gate = gate
        self.monitor = monitor or DivergenceMonitor()
        self.name = f"shadow({getattr(baseline, 'name', '?')}|{getattr(learned, 'name', '?')})"
        self.blocked: list[str] = []

    def decide(self, *args, **kwargs) -> tuple[Action, float | None]:
        decision = self.decide_q(*args, **kwargs)
        return decision.action, decision.alpha

    def decide_q(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: PathwayOptions | None = None,
    ) -> Decision:
        acted = _ask(
            self._baseline, candidate, utility, ceiling, round_state, budget, snapshot, pathways
        )
        shadow = _ask(
            self._learned, candidate, utility, ceiling, round_state, budget, snapshot, pathways
        )

        if self._gate is not None:
            verdict = self._gate.check(
                shadow, candidate, utility, ceiling, pathways,
                news2=getattr(pathways, "baseline_news2", None),
            )
            if not verdict.allowed:
                self.blocked.append(verdict.rule)
                shadow = verdict.substituted or shadow

        self.monitor.record(acted, shadow)
        return acted


class GatedPolicy:
    """A learned policy that acts, with the gates in front of it.

    **Only for a supervised pilot, and only after a shadow period.** Promotion from
    :class:`ShadowPolicy` to this class is the point at which a learned policy first influences
    a real allocation, and it is a governance decision — the gates below make it survivable,
    not advisable.
    """

    def __init__(
        self,
        learned: BiddingPolicy,
        gate: SafetyGate,
        fallback: BiddingPolicy | None = None,
    ) -> None:
        self._learned = learned
        self._gate = gate
        self._fallback = fallback
        self.name = f"gated({getattr(learned, 'name', '?')})"
        self.blocked: list[str] = []

    def decide(self, *args, **kwargs) -> tuple[Action, float | None]:
        decision = self.decide_q(*args, **kwargs)
        return decision.action, decision.alpha

    def decide_q(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: PathwayOptions | None = None,
    ) -> Decision:
        decision = _ask(
            self._learned, candidate, utility, ceiling, round_state, budget, snapshot, pathways
        )
        verdict = self._gate.check(
            decision, candidate, utility, ceiling, pathways,
            news2=getattr(pathways, "baseline_news2", None),
        )
        if verdict.allowed:
            return decision

        self.blocked.append(verdict.rule)
        if self._fallback is not None:
            return _ask(
                self._fallback, candidate, utility, ceiling, round_state, budget,
                snapshot, pathways,
            )
        return verdict.substituted or decision


def _ask(
    policy: BiddingPolicy,
    candidate: Candidate,
    utility: UtilityBreakdown,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    snapshot: FeatureSnapshot,
    pathways: PathwayOptions | None,
) -> Decision:
    """Ask a policy through the widest seam it implements."""
    decide_q = getattr(policy, "decide_q", None)
    if decide_q is not None:
        return decide_q(candidate, utility, ceiling, round_state, budget, snapshot, pathways)
    action, alpha = policy.decide(candidate, utility, ceiling, round_state, budget, snapshot)
    if action is Action.WITHDRAW:
        return Decision(q_action=QAction.WITHDRAW_UNPLANNED, action=Action.WITHDRAW)
    return Decision.compete(QAction.CONTINUE, action, alpha)
