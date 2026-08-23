"""A learned policy: linear Q over the six actions, plus an aggression head.

    Q(s, a)  =  w_a · s + b_a          one weight row per action
    alpha(s) =  sigmoid(v · s + c)     how hard to bid, when the choice is to bid

Two heads rather than one, because the framework separates the two questions and the auction
enforces the separation. ``QAction`` decides *what to do about the patient*; ``alpha`` decides
*how much headroom to expose*, and only matters for the two non-exiting actions. Folding them
into one output would make the exits carry a meaningless magnitude and the bid carry a
meaningless label.

**Linear, and stopping there deliberately.** RL_READINESS §5.3 ② gives the ladder as "parametric
rules + CEM → tabular Q → linear Q → network". At roughly six auctions per department per shift,
a network has orders of magnitude more parameters than the data supports, and — the reason that
actually decides it — a linear model's weights are *readable*. When the fabrication sweep in
``evaluate.py`` asks whether the policy learned the structure or the outcome model, a weight
vector over twenty-two named features can be inspected and argued about. A network's cannot.

**Infeasible actions are masked, not penalised.** An alternative that does not exist is not a
bad choice, it is not a choice. Masking with ``-inf`` before the argmax means the policy is
never trained to avoid something it could not have done, and — more importantly — a
:class:`Decision` for an unavailable exit would fail to construct anyway, because it could not
name its plan.

**Guards stay outside.** ``auction/guards.py`` clamps to the ceiling, to affordability and to
whole points, *after* the policy speaks. ``policy/__init__.py`` states why: *"A constraint
enforced inside a policy is a constraint a learned policy can be trained to violate, whenever
violating it once paid off."* Nothing here re-implements a guard.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    PathwayPlan,
    QAction,
    RoundState,
    UtilityBreakdown,
)
from allocation.pathway.plans import build_plan
from allocation.rl.encoder import ACTION_INDEX, ACTIONS, SIZE, StateEncoder

#: Weights per action, plus the alpha head. Flattened, this is what CEM searches over.
PARAM_COUNT = len(ACTIONS) * (SIZE + 1) + (SIZE + 1)


@dataclass(frozen=True, slots=True)
class QWeights:
    """The parameters. Immutable, versioned against the encoder they were fitted for."""

    rows: tuple[tuple[float, ...], ...]      # one per action, length SIZE
    biases: tuple[float, ...]                # one per action
    alpha_row: tuple[float, ...]             # length SIZE
    alpha_bias: float
    encoder_version: str
    #: The simulated world these were fitted in. A policy is only valid for the fabrication it
    #: trained against — the same argument as the encoder version, and the same failure if
    #: ignored: it runs, emits plausible numbers, and is about a different world.
    fabrication_version: str = ""
    policy_version: str = "rl-linear-v1"

    @classmethod
    def zeros(cls, encoder_version: str, fabrication_version: str = "") -> "QWeights":
        return cls(
            rows=tuple(tuple(0.0 for _ in range(SIZE)) for _ in ACTIONS),
            biases=tuple(0.0 for _ in ACTIONS),
            alpha_row=tuple(0.0 for _ in range(SIZE)),
            alpha_bias=0.0,
            encoder_version=encoder_version,
            fabrication_version=fabrication_version,
        )

    # -- flat vector, for the optimiser ------------------------------------------------

    def flat(self) -> tuple[float, ...]:
        out: list[float] = []
        for row, bias in zip(self.rows, self.biases):
            out.extend(row)
            out.append(bias)
        out.extend(self.alpha_row)
        out.append(self.alpha_bias)
        return tuple(out)

    @classmethod
    def from_flat(
        cls, values: Sequence[float], encoder_version: str, fabrication_version: str = ""
    ) -> "QWeights":
        if len(values) != PARAM_COUNT:
            raise ValueError(f"expected {PARAM_COUNT} parameters, got {len(values)}")
        rows: list[tuple[float, ...]] = []
        biases: list[float] = []
        cursor = 0
        for _ in ACTIONS:
            rows.append(tuple(values[cursor : cursor + SIZE]))
            biases.append(float(values[cursor + SIZE]))
            cursor += SIZE + 1
        return cls(
            rows=tuple(rows),
            biases=tuple(biases),
            alpha_row=tuple(values[cursor : cursor + SIZE]),
            alpha_bias=float(values[cursor + SIZE]),
            encoder_version=encoder_version,
            fabrication_version=fabrication_version,
        )

    # -- persistence -------------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "policy_version": self.policy_version,
                    "encoder_version": self.encoder_version,
                    "fabrication_version": self.fabrication_version,
                    "actions": [a.value for a in ACTIONS],
                    "rows": [list(r) for r in self.rows],
                    "biases": list(self.biases),
                    "alpha_row": list(self.alpha_row),
                    "alpha_bias": self.alpha_bias,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return out

    @classmethod
    def load(cls, path: str | Path, encoder: StateEncoder | None = None) -> "QWeights":
        """Load, refusing a mismatched encoding.

        A policy served a different state vector is not degraded, it is undefined — the
        parameters mean different things. This is the check that F-24 exists to demand.
        """
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        weights = cls(
            rows=tuple(tuple(float(x) for x in r) for r in body["rows"]),
            biases=tuple(float(x) for x in body["biases"]),
            alpha_row=tuple(float(x) for x in body["alpha_row"]),
            alpha_bias=float(body["alpha_bias"]),
            encoder_version=str(body["encoder_version"]),
            fabrication_version=str(body.get("fabrication_version", "")),
            policy_version=str(body.get("policy_version", "rl-linear-v1")),
        )
        current = (encoder or StateEncoder()).version
        if weights.encoder_version != current:
            raise ValueError(
                f"policy was fitted under encoder {weights.encoder_version} but this build "
                f"encodes as {current}. The vector's features have changed, so the weights "
                "refer to different quantities. Refit rather than reinterpret."
            )
        if [a.value for a in ACTIONS] != list(body["actions"]):
            raise ValueError(
                "the action ordering has changed since this policy was fitted; a Q-vector is "
                "only interpretable against the ordering it was trained on."
            )
        return weights


class LinearQPolicy:
    """A trained bidding policy, behind the same seams as the heuristic.

    Implements both ``decide_q`` (six actions, with plans) and ``decide`` (bid mechanics only),
    so it drops into the existing auction with nothing outside ``policy/`` knowing which is
    running — the property ``policy/__init__.py`` requires.
    """

    def __init__(
        self,
        config: Config,
        weights: QWeights,
        encoder: StateEncoder | None = None,
        name: str | None = None,
    ) -> None:
        self._config = config
        self._weights = weights
        self._encoder = encoder or StateEncoder()
        if weights.encoder_version and weights.encoder_version != self._encoder.version:
            raise ValueError(
                f"weights encode as {weights.encoder_version}, this encoder as "
                f"{self._encoder.version}"
            )
        self.name = name or f"rl:{weights.policy_version}"
        self._min_release_p = float(
            config.rule("pathway")["next_release"]["min_probability"]
        )

    @property
    def weights(self) -> QWeights:
        return self._weights

    # -- the seams ---------------------------------------------------------------------

    def decide(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
    ) -> tuple[Action, float | None]:
        decision = self.decide_q(
            candidate, utility, ceiling, round_state, budget, snapshot, pathways=None
        )
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
        state = self._encode(candidate, utility, ceiling, round_state, budget, snapshot, pathways)
        feasible = self._feasible(candidate, ceiling, round_state, pathways)
        q_values = self._q(state)

        # Mask, do not penalise. An exit whose plan cannot be named would fail to construct
        # anyway, so choosing it is not a mistake to be trained out — it is impossible.
        best = max(feasible, key=lambda a: q_values[a])
        published = {a: q for a, q in q_values.items() if a in feasible}

        if not best.exits:
            alpha = self._alpha(state)
            action = Action.INCREASE_BID if alpha > 1e-6 else Action.HOLD
            return Decision(
                q_action=best,
                action=action,
                alpha=alpha if action is Action.INCREASE_BID else None,
                q_values=published,
                feasible=feasible,
            )

        return Decision(
            q_action=best,
            action=Action.WITHDRAW,
            plan=self._plan(best, pathways),
            q_values=published,
            feasible=feasible,
        )

    # -- the model ---------------------------------------------------------------------

    def _q(self, state: Sequence[float]) -> dict[QAction, float]:
        w = self._weights
        return {
            action: sum(x * s for x, s in zip(w.rows[i], state)) + w.biases[i]
            for action, i in ACTION_INDEX.items()
        }

    def _alpha(self, state: Sequence[float]) -> float:
        """Aggression in ``[0, 1]``, through a logistic.

        Squashed rather than clipped so the optimiser sees a gradient everywhere: a clipped
        head is flat outside its range, and CEM's elite set would carry no information about
        which direction to move parameters that are saturated.
        """
        z = sum(x * s for x, s in zip(self._weights.alpha_row, state)) + self._weights.alpha_bias
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    # -- feasibility and plans ---------------------------------------------------------

    def _feasible(
        self,
        candidate: Candidate,
        ceiling: float,
        round_state: RoundState,
        pathways: PathwayOptions | None,
    ) -> frozenset[QAction]:
        """Which actions could actually be taken now.

        ``WITHDRAW_UNPLANNED`` is always feasible, and that is not a modelling convenience: a
        patient who cannot win, has no alternative and has no predicted bed *is* abandoned, and
        the action space has to be able to say so.
        """
        out = {QAction.WITHDRAW_UNPLANNED}

        mine = next(
            (b.amount for b in round_state.bids
             if b.agent is candidate.agent and b.action is not Action.WITHDRAW),
            0.0,
        )
        rivals = [
            b.amount for b in round_state.bids
            if b.agent is not candidate.agent and b.action is not Action.WITHDRAW
        ]
        # Cannot win at full stretch (§12), or already above the ceiling (§16). Competing is
        # not an option the policy is allowed to prefer — this is the one place a *mechanical*
        # fact overrides the value estimate, and it mirrors the heuristic's rules 1 and 2.
        can_compete = mine <= ceiling + 1e-9 and not (rivals and ceiling <= max(rivals) + 1e-9)
        if can_compete:
            out |= {QAction.WIN_NOW, QAction.CONTINUE}

        if pathways is None:
            return frozenset(out)

        best = getattr(pathways, "best_alternative", None)
        if best is not None:
            out.add(QAction.WITHDRAW_ALTERNATIVE)

        probability = pathways.next_release_probability
        if probability is not None and probability >= self._min_release_p:
            out.add(QAction.AWAIT_NEXT_RESOURCE)

        make = getattr(pathways, "make_reentry", None)
        if make is not None and make(best.unit if best else None) is not None:
            out.add(QAction.RE_ENTER_LATER)

        return frozenset(out)

    def _plan(self, action: QAction, pathways: PathwayOptions | None) -> PathwayPlan | None:
        """Delegated to ``pathway.plans`` so every caller builds a plan the same way."""
        return build_plan(action, pathways, note=f"learned {action.value}")

    def _encode(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: PathwayOptions | None,
    ) -> tuple[float, ...]:
        """Encode from the round view the policy is given.

        A shim rather than a second encoder: ``StateEncoder.encode`` takes an ``AuctionResult``
        because that is what the dataset writer has, and mid-auction the policy has a
        ``RoundState`` instead. The adapter below presents the same three attributes the
        encoder reads, so both paths produce the *same vector for the same situation* — which
        is the only property that makes a trained policy valid at serving time.
        """
        return self._encoder.encode(
            agent=candidate.agent,
            utility=utility.total,
            ceiling=ceiling,
            budget=budget,
            result=_RoundView(round_state),
            snapshot=snapshot,
            options=pathways,
            round_index=round_state.round_index,
        )


@dataclass(frozen=True, slots=True)
class _RoundView:
    """Presents a :class:`RoundState` with the attributes the encoder reads off a result."""

    round_state: RoundState

    @property
    def positions(self) -> Mapping[AgentKind, Any]:
        return {
            bid.agent: _Standing(bid.amount)
            for bid in self.round_state.bids
            if bid.action is not Action.WITHDRAW
        }

    @property
    def contention(self) -> float:
        live = [b.contention for b in self.round_state.bids if b.contention is not None]
        return live[0] if live else 1.0

    @property
    def rounds_run(self) -> int:
        return self.round_state.round_index + 1

    @property
    def max_rounds(self) -> int:
        return 3  # the profile cadence; rounds_left is a coarse feature either way


@dataclass(frozen=True, slots=True)
class _Standing:
    current_bid: float
