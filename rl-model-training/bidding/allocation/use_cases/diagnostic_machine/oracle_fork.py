"""Diagnostic-only counterfactual fork policy, and the trace collector it is targeted from.

**Not used by a served production path.** The controlled comparison uses its trace collector
and forced-decision builder; the same seam can ask, at
one real decision point: *if the request had taken this other feasible action instead, what
would have happened?* — with every other decision, this request's own past and future rounds,
every other request, and every other agent, left exactly as :class:`.policy.DiagnosticHeuristicPolicy`
would have made them.

**Targeting is by (request_id, step), not by auction ordinal.** Earlier bed studies targeted
one agent's Nth auction because a bed agent's identity
persists across many independent auctions. A diagnostic request instead owns one trajectory
end to end — :mod:`.dataset` already keys transitions this way (``DiagnosticTransition.step``)
— so counting how many times *this specific request* has been asked to decide is both the
natural target key and the one the rest of this package already uses.

**Fork fidelity needs no simulator surgery**, and for a stronger reason than the bed case: see
``random_worlds.py``'s module docstring — :func:`.simulator.simulate` has no internal RNG to
keep synchronised between branches. It is already a pure function of its arguments, so two
calls that agree on every decision up to some point are byte-identical up to that point with no
special handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind, BudgetState, RoundState
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.encoder import EncodedState, encode
from allocation.use_cases.diagnostic_machine.ladder import Verdict, climb
from allocation.use_cases.diagnostic_machine.masks import ACTIONS, INDEX, mask
from allocation.use_cases.diagnostic_machine.policy import (
    DiagnosticDecision,
    DiagnosticHeuristicPolicy,
    DiagnosticOptions,
    DiagnosticPlan,
)

#: Index of ``is_leading`` in :data:`.encoder.FEATURES` — used to classify a decision's bidding
#: regime for coverage reporting. Looked up once by name rather than hardcoded, so a reordering
#: of ``FEATURES`` (which changes ``encoder_version()``) cannot silently desync this too.
from allocation.use_cases.diagnostic_machine.encoder import FEATURES

_IS_LEADING_INDEX = FEATURES.index("is_leading")


def bidding_regime(round_index: int, state_values: Sequence[float]) -> str:
    """``opening`` / ``leading`` / ``overtaking`` — the diagnostic analogue of the bed
    encoder's ``regime_of``, over the features this package actually has."""
    if round_index == 0:
        return "opening"
    return "leading" if state_values[_IS_LEADING_INDEX] >= 0.5 else "overtaking"


@dataclass(frozen=True, slots=True)
class DecisionStep:
    """One recorded decision, before it is acted on."""

    state: EncodedState
    action_index: int
    action_mask: tuple[bool, ...]
    round_index: int
    now: datetime


@dataclass
class TraceCollector:
    """A ``decision_hook`` that records every decision, per request, in call order.

    Public and diagnostic-only — unlike :mod:`.dataset`'s private ``_Collector``, which this
    does not import or subclass, so a change to the training dataset's internals cannot silently
    break the audit or vice versa.
    """

    steps: dict[str, list[DecisionStep]] = field(default_factory=dict)
    agents: dict[str, AgentKind] = field(default_factory=dict)

    def __call__(
        self,
        *,
        agent: AgentKind,
        request,
        round_index: int,
        utility,
        ceiling: float,
        view,
        budget,
        options: DiagnosticOptions,
        contention: float,
        max_rounds: int,
        decision: DiagnosticDecision,
    ) -> None:
        state = encode(
            utility=utility,
            ceiling=ceiling,
            round_state=view,
            budget=budget,
            options=options,
            agent=agent,
            max_rounds=max_rounds,
            contention=contention,
            now=options.now,
        )
        allowed = mask(decision.feasible)
        index = INDEX[decision.pathway]
        self.steps.setdefault(request.request_id, []).append(
            DecisionStep(
                state=state, action_index=index, action_mask=allowed,
                round_index=round_index, now=options.now,
            )
        )
        self.agents[request.request_id] = agent


def _opening_alpha(config: Config, agent: AgentKind) -> float:
    """The configured opening aggression for ``agent`` — the same table
    :class:`.policy.DiagnosticHeuristicPolicy` reads, and one of :func:`climb`'s three tuning
    inputs (with :func:`_lead_alpha`/:func:`_overtake_margin`) used in :func:`build_forced_decision`'s
    compete branch."""
    cfg = config.auction["policy"]["heuristic"]["opening_alpha"]
    return float(cfg.get(agent.value, cfg["default"]))


def _lead_alpha(config: Config) -> float:
    """Same table, same key :class:`.policy.DiagnosticHeuristicPolicy` reads as ``self._lead_alpha``."""
    return float(config.auction["policy"]["heuristic"]["lead_alpha"])


def _overtake_margin(config: Config) -> float:
    """Same table, same key :class:`.policy.DiagnosticHeuristicPolicy` reads as ``self._margin``."""
    return float(config.auction["policy"]["heuristic"]["overtake_margin"])


def _best_available_exit(
    feasible: frozenset[DiagnosticPathwayAction],
) -> DiagnosticPathwayAction:
    """Mirrors ``.serving.ServedQPolicy._best_available_exit`` exactly. Duplicated rather than
    imported, per this package's own established precedent (see this module's and ``ladder.py``'s
    docstrings): a shared helper would create a new dependency edge from this lower-level module
    onto the serving seam, for four lines that ``tests/test_diagnostic_ladder.py`` already pins
    against ``serving.py`` so a divergence is a test failure, not a silent drift.
    """
    for candidate in (
        DiagnosticPathwayAction.USE_ALTERNATIVE,
        DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY,
        DiagnosticPathwayAction.RE_ENTER_LATER,
    ):
        if candidate in feasible:
            return candidate
    return DiagnosticPathwayAction.WITHDRAW_UNPLANNED


def build_forced_decision(
    config: Config,
    forced: DiagnosticPathwayAction,
    agent: AgentKind,
    options: DiagnosticOptions,
    feasible: frozenset[DiagnosticPathwayAction],
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
) -> DiagnosticDecision:
    """A valid :class:`DiagnosticDecision` for ``forced``, honouring every invariant
    ``DiagnosticDecision.__post_init__`` enforces. Only called for actions already confirmed
    feasible, so every branch below has what it needs.

    ``ceiling``/``round_state``/``budget`` exist for exactly one reason: a forced WIN_NOW/CONTINUE
    must be sized and gated by :func:`climb` — the SAME dynamic, round-state-dependent ladder
    :class:`.serving.ServedQPolicy` runs — not a static opening alpha. Before this, every
    experimental caller (the collection explorer, ``ServedLinearPolicy``, ``OracleForkPolicy``'s
    forced substitution) priced every forced compete at the SAME opening aggression regardless of
    whether the round was actually opening, leading or trailing, and never applied the ladder's
    mechanical affordability/ceiling exits either.
    """
    if forced is DiagnosticPathwayAction.WITHDRAW_UNPLANNED:
        return DiagnosticDecision(pathway=forced, feasible=feasible)
    if forced is DiagnosticPathwayAction.USE_ALTERNATIVE:
        best = options.best_alternative()
        assert best is not None, "USE_ALTERNATIVE was feasible but best_alternative() is None"
        modality, value = best
        plan = DiagnosticPlan(
            alternative_modality=modality, alternative_yield=value,
            note="oracle audit: forced use_alternative",
        )
        return DiagnosticDecision(pathway=forced, plan=plan, feasible=feasible)
    if forced is DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY:
        plan = DiagnosticPlan(
            expected_capacity_at=options.next_capacity_at,
            capacity_probability=options.next_capacity_probability,
            note="oracle audit: forced await_next_capacity",
        )
        return DiagnosticDecision(pathway=forced, plan=plan, feasible=feasible)
    if forced is DiagnosticPathwayAction.RE_ENTER_LATER:
        plan = DiagnosticPlan(
            reentry_condition="oracle audit forced re-entry",
            note="oracle audit: forced re_enter_later",
        )
        return DiagnosticDecision(pathway=forced, plan=plan, feasible=feasible)

    # WIN_NOW or CONTINUE: both compete. The ladder is authoritative for HOW MUCH (dynamic
    # alpha, exactly like .serving.ServedQPolicy._compete) and for whether competing is even
    # mechanically possible — its EXIT verdicts override the forced action, same as serving.
    outcome = climb(
        config, agent=agent, ceiling=ceiling, round_state=round_state, budget=budget,
        opening_alpha=_opening_alpha(config, agent), lead_alpha=_lead_alpha(config),
        overtake_margin=_overtake_margin(config),
    )
    if outcome.verdict is Verdict.EXIT:
        # Mechanical override, not a preference: recurse into the branch above for the best
        # available exit — it already builds a proper plan, so nothing here duplicates that.
        return build_forced_decision(
            config, _best_available_exit(feasible), agent, options, feasible,
            ceiling, round_state, budget,
        )
    if outcome.verdict is Verdict.HOLD:
        return DiagnosticDecision(
            pathway=DiagnosticPathwayAction.CONTINUE, feasible=feasible, holds=True
        )
    return DiagnosticDecision(pathway=forced, alpha=outcome.alpha, feasible=feasible)


class OracleForkPolicy:
    """:class:`.policy.DiagnosticHeuristicPolicy` everywhere, except at exactly one
    ``(target_request_id, target_step)``, where it substitutes ``forced_action`` — or, with
    ``forced_action=None``, nowhere at all (used as the no-op fidelity check).

    ``target_step`` counts calls to :meth:`decide` for ``target_request_id`` in the order they
    happen, matching :attr:`.dataset.DiagnosticTransition.step`'s indexing exactly.
    """

    def __init__(
        self,
        config: Config,
        target_request_id: str | None,
        target_step: int | None,
        forced_action: DiagnosticPathwayAction | None,
    ) -> None:
        self._config = config
        self._heuristic = DiagnosticHeuristicPolicy(config)
        self._target_request_id = target_request_id
        self._target_step = target_step
        self._forced_action = forced_action
        self._step_by_request: dict[str, int] = {}
        #: Set True the one time (if any) the fork actually substituted an action, so a caller
        #: can confirm the target was reached rather than silently auditing nothing.
        self.fired = False

    def decide(
        self,
        agent: AgentKind,
        ceiling: float,
        round_state,
        budget,
        options: DiagnosticOptions,
        **context: object,
    ) -> DiagnosticDecision:
        default_decision = self._heuristic.decide(
            agent, ceiling, round_state, budget, options, **context
        )
        request_id = options.request.request_id
        step = self._step_by_request.get(request_id, -1) + 1
        self._step_by_request[request_id] = step

        if (
            self._forced_action is None
            or request_id != self._target_request_id
            or step != self._target_step
        ):
            return default_decision

        if self._forced_action not in default_decision.feasible:
            # Not actually available at this (freshly re-derived) state — report honestly by
            # falling back rather than fabricating an impossible decision.
            return default_decision

        self.fired = True
        if self._forced_action is default_decision.pathway:
            # Replaying the heuristic's own choice: return its own decision unchanged, so this
            # is byte-identical to the baseline by construction, not merely "close".
            return default_decision

        return build_forced_decision(
            self._config, self._forced_action, agent, options, default_decision.feasible,
            ceiling, round_state, budget,
        )


__all__ = [
    "DecisionStep",
    "OracleForkPolicy",
    "TraceCollector",
    "bidding_regime",
    "build_forced_decision",
]
