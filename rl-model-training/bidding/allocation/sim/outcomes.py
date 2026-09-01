"""The outcome model. **The largest fabrication in the system, and it is the objective.**

RL_READINESS §7.7 on this module's subject matter, verbatim: *"Outcome model — fitted from:
nothing available — this is B.10. Pure fabrication, and it becomes the objective the policy
optimises."* Everything below is invented. It cannot be otherwise: mortality has no column
anywhere in ninety migrations (F-01), and no amount of care in writing this file changes that.

What this module *can* do honestly is three things.

**Produce a real reward signal, so episodes complete.** ``reward/observer.score`` already
implements the three-state rule correctly — observed / not-observed / missing — and
``reward/episode.trainable`` already returns nothing, because ``no_mortality`` is never
observed. That is the correct output against a real hospital and it is also a permanently empty
training set. Here mortality *is* observable, because here it is simulated, so episodes complete
and the discounted return of §21 becomes computable. Nothing about the scoring path is bypassed:
this module emits the same ``{term: bool | None}`` map an ``ObservationSource`` would.

**Score the counterfactual, not just the winner.** §24 exists because a losing episode is
training data too. A patient who was not placed still has an outcome, and it is the comparison
between the two that carries all the signal. Scoring only the winner would leave the policy
unable to learn that losing was survivable.

**Stay separable from what the policy observes.** Every value here is read from the latent
severity that the *policy never sees* — the policy sees rendered vitals through the real feature
layer. Where a reward term could be read off an input the policy also sees, it is deliberately
decoupled: ``second_bed_opened`` uses its own probability rather than the next-release forecast
the policy consults, because a policy that could read its own reward off its own observation
would be learning this simulator's plumbing rather than any allocation structure.

**The one thing this module must never be used for.** RL_READINESS §2.B: *"a simulator
comparison answers which policy captures the pacing structure better. It never answers which
policy saves more patients."* The mortality numbers here are a mechanism for making the
optimisation well-posed. They are not an estimate of anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from random import Random
from typing import Mapping, Sequence

from allocation.audit.records import OutcomeRow
from allocation.config import Config
from allocation.contracts import AgentKind, QAction
from allocation.reward.observer import score
from allocation.sim.fabricated import FabricationRegister
from allocation.sim.patients import SimPatient


@dataclass(frozen=True, slots=True)
class Fate:
    """What happened to one patient inside the outcome window.

    Retained per patient rather than collapsed straight into reward terms because the episode
    log needs to be re-scoreable: the reward *weights* are unsigned config and will change, and
    an episode that stored only the total could not be re-scored under new ones.
    """

    candidate_id: str
    agent: AgentKind
    placed_unit: str | None
    severity_at_close: float
    severity_at_horizon: float
    died: bool
    deteriorated: bool
    escalated: bool
    stabilised: bool
    #: What the agent's exit arranged, when it left the auction. ``None`` when it competed to
    #: the close. This is what makes ``safely_held`` attributable — see :func:`observations`.
    exit_action: QAction | None = None


def advance_to_horizon(
    patient: SimPatient,
    hours: float,
    fab: FabricationRegister,
    rng: Random,
    steps: int = 8,
) -> SimPatient:
    """Roll the latent state forward over the outcome window.

    In steps rather than one jump: a single large step with one noise draw would let a patient
    teleport across the critical threshold, and the intermediate crossings are what
    ``deteriorated`` and ``escalated`` are about.
    """
    out = patient
    for _ in range(steps):
        out = out.advanced(hours / steps, fab, rng)
    return out


def resolve(
    patient: SimPatient,
    horizon_hours: float,
    fab: FabricationRegister,
    rng: Random,
    exit_action: QAction | None = None,
) -> Fate:
    """What becomes of one patient over the horizon.

    Mortality is drawn against severity **integrated over the window**, not against the severity
    at close. A patient placed immediately and one placed after three hours of deterioration can
    end the window at the same severity, and treating them identically would erase the entire
    cost of delay — which is the quantity the policy is supposed to be learning to weigh.
    """
    trace = [patient.severity]
    current = patient
    steps = 8
    for _ in range(steps):
        current = current.advanced(horizon_hours / steps, fab, rng)
        trace.append(current.severity)

    mean_severity = sum(trace) / len(trace)
    peak = max(trace)

    risk = fab["outcome.mortality_base"] + fab["outcome.mortality_severity_slope"] * mean_severity
    died = rng.random() < max(0.0, min(1.0, risk))

    critical = fab["severity.critical_threshold"]
    return Fate(
        candidate_id=patient.candidate.candidate_id,
        agent=patient.candidate.agent,
        placed_unit=patient.placed_unit,
        severity_at_close=patient.severity,
        severity_at_horizon=current.severity,
        died=died,
        deteriorated=current.severity > patient.severity and peak >= critical,
        escalated=peak >= fab["outcome.escalation_threshold"] and patient.placed_unit is None,
        stabilised=current.severity <= fab["outcome.stabilised_threshold"],
        exit_action=exit_action,
    )


def observations(
    fates: Sequence[Fate],
    winner: AgentKind | None,
    fab: FabricationRegister,
    rng: Random,
) -> tuple[Mapping[str, bool | None], bool]:
    """Reward-term observations for one auction, and whether anyone died.

    Returns exactly what an ``ObservationSource`` returns — ``{term: True | False | None}`` — so
    the real scorer runs unchanged. ``None`` is used where a term genuinely does not apply: a
    theatre term on an auction with no surgical bidder is not an observation gap, and
    ``observer.score`` already distinguishes the two.

    **The attribution rules are the point of the Q-space.** ``safely_held`` and
    ``second_bed_opened`` are only awarded when an agent actually *chose* the exit that produces
    them. Before the six-action space these attached to whichever agent happened to have bid,
    which meant the objective paid for a hand-off no policy selected — and any policy trained on
    that learns to credit a bid for a pathway decision.
    """
    won = [f for f in fates if f.placed_unit == "icu"]
    lost = [f for f in fates if f.placed_unit != "icu"]
    by_action = {f.exit_action for f in fates if f.exit_action is not None}
    surgical = any(f.agent is AgentKind.OT for f in fates)

    obs: dict[str, bool | None] = {}

    # A term that CANNOT APPLY is OMITTED, never set to None. `observer.score` treats an
    # explicit None as an observation gap and marks the episode incomplete — correctly, because
    # a term that could have applied and was not read is a gap. But a theatre term on an auction
    # with no surgical bidder never could have applied, and passing None for it would discard a
    # perfectly good episode. `score` already draws exactly this distinction ("silence about a
    # term that was never passed in is not counted against the episode"); this function has to
    # hold up its end.
    def observe(name: str, value: bool | None, applies: bool = True) -> None:
        if applies and value is not None:
            obs[name] = value

    # --- §23 · the episode where the bed was awarded ---------------------------------
    observe("transferred_to_icu", bool(won))
    observe("patient_stabilised", all(f.stabilised for f in won), applies=bool(won))
    observe(
        "boarding_reduced",
        rng.random() < fab["outcome.boarding_relief_probability"]
        if any(f.agent is AgentKind.ER for f in won)
        else False,
    )
    observe("cubicle_released", bool(won))

    # ATTRIBUTED, not inferred. +10 for a losing surgical case safely held in PACU — awarded
    # only when an agent chose WITHDRAW_ALTERNATIVE, never merely because a patient happened
    # to end up somewhere.
    observe("safely_held", QAction.WITHDRAW_ALTERNATIVE in by_action, applies=surgical)

    # Also attributed: waiting on a predicted release is a decision, and the bed materialising
    # is the outcome that decision was betting on.
    observe(
        "second_bed_opened",
        rng.random() < fab["outcome.second_bed_probability"]
        if QAction.AWAIT_NEXT_RESOURCE in by_action
        else False,
    )
    observe("surgery_not_cancelled", _surgery_survived(fates, fab, rng), applies=surgical)
    observe("no_staffing_violation", True)

    # --- §24 · the counterfactual, for the patients who did not get the bed ----------
    observe("patient_deterioration", any(f.deteriorated for f in lost), applies=bool(lost))
    observe(
        "additional_boarding",
        any(f.agent is AgentKind.ER and f.placed_unit is None for f in lost),
        applies=bool(lost),
    )
    observe("emergency_escalation", any(f.escalated for f in lost), applies=bool(lost))
    observe(
        "ot_throughput",
        any(f.agent is AgentKind.OT and f.placed_unit is not None for f in lost),
        applies=surgical,
    )
    observe("revenue", bool(won))

    # Mortality never travels through the observation map. It sets the sign of the episode, so
    # `observer.score` takes it as a separate argument that cannot be set by something that
    # merely forgot to mention it.
    no_mortality = not any(f.died for f in fates)
    return obs, no_mortality


def _surgery_survived(
    fates: Sequence[Fate], fab: FabricationRegister, rng: Random
) -> bool:
    """Was the theatre case spared? Only at risk when OT lost with nothing arranged."""
    for fate in fates:
        if fate.agent is not AgentKind.OT:
            continue
        if fate.placed_unit is not None:
            return True
        if fate.exit_action is QAction.WITHDRAW_ALTERNATIVE:
            return True  # PACU took it; that is what the alternative bought
        return rng.random() >= fab["outcome.cancellation_probability"]
    return True


def observations_for(
    fate: Fate,
    fates: Sequence[Fate],
    fab: FabricationRegister,
    rng: Random,
) -> tuple[Mapping[str, bool | None], bool]:
    """Reward-term observations **from one agent's point of view**. F-23.

    RL_READINESS §5.3 ① lists *"reward per-agent or global?"* as a decision required before any
    code is written: §22 gives each department its own objective, §23 computes one scalar, and
    independent learners / shared reward / CTDE are different algorithms.

    The first implementation here used §23's single scalar and handed it to every bidder. That
    is the shared-reward arm, and with no per-agent credit it is a **free-rider game**: an agent
    collects the same reward whether it wins or loses, while winning costs budget. A CEM policy
    trained against it duly learned to stop competing — ``win_now`` weighted −0.80 against
    ``withdraw_alternative`` +0.35, scoring above the heuristic on a 4 % burn and a 5 % win
    share. It was playing the reward correctly; the reward was wrong.

    So credit is assigned per agent, and ``reward.yaml``'s own structure says how: every term is
    tagged ``scenario: won`` or ``scenario: lost``, which are exactly the two perspectives. An
    agent that got the bed is scored on what happened to *its* patient in ICU; an agent that did
    not is scored on what happened to *its* patient without it.

    What stays shared is only what is genuinely shared — ED boarding is a fact about the
    department, not about one patient.
    """
    won = fate.placed_unit == "icu"
    mine_surgical = fate.agent is AgentKind.OT
    obs: dict[str, bool | None] = {}

    def observe(name: str, value: bool | None, applies: bool = True) -> None:
        if applies and value is not None:
            obs[name] = value

    if won:
        # --- §23 · this agent got the bed ---------------------------------------------
        observe("transferred_to_icu", True)
        observe("patient_stabilised", fate.stabilised)
        observe("cubicle_released", True)
        observe("revenue", True)
        observe(
            "boarding_reduced",
            rng.random() < fab["outcome.boarding_relief_probability"],
            applies=fate.agent is AgentKind.ER,
        )
        observe("surgery_not_cancelled", True, applies=mine_surgical)
        observe("no_staffing_violation", True)
        # Attributed to a chosen action, not to the situation. Only meaningful for a *losing*
        # surgical case, so it cannot apply to the agent that won.
        observe(
            "second_bed_opened",
            rng.random() < fab["outcome.second_bed_probability"],
            applies=fate.exit_action is QAction.AWAIT_NEXT_RESOURCE,
        )
    else:
        # --- §24 · this agent did not get the bed -------------------------------------
        observe("transferred_to_icu", False)
        observe("patient_deterioration", fate.deteriorated)
        observe("emergency_escalation", fate.escalated)
        observe(
            "additional_boarding",
            fate.placed_unit is None,
            applies=fate.agent is AgentKind.ER,
        )
        # The two exits that arranged something, credited only when they were chosen.
        observe(
            "safely_held",
            fate.exit_action is QAction.WITHDRAW_ALTERNATIVE,
            applies=mine_surgical,
        )
        observe(
            "second_bed_opened",
            rng.random() < fab["outcome.second_bed_probability"],
            applies=fate.exit_action is QAction.AWAIT_NEXT_RESOURCE,
        )
        observe(
            "ot_throughput", fate.placed_unit is not None, applies=mine_surgical
        )
        observe(
            "surgery_not_cancelled",
            _surgery_survived([fate], fab, rng),
            applies=mine_surgical,
        )
        observe("no_staffing_violation", True)

    # Mortality is about THIS agent's patient, and travels separately because it sets the sign
    # of the episode and must never be settable by something that merely forgot to mention it.
    return obs, not fate.died


def score_for_agent(
    config: Config,
    auction_id: str,
    fate: Fate,
    fates: Sequence[Fate],
    fab: FabricationRegister,
    rng: Random,
    observed_at: datetime | None = None,
) -> OutcomeRow:
    """One agent's reward, through the real scorer."""
    obs, no_mortality = observations_for(fate, fates, fab, rng)
    return score(
        config,
        auction_id=auction_id,
        observations=obs,
        observed_at=observed_at,
        mortality_observed=no_mortality,
        mortality_source=f"simulated:{fab.version}",
    )


def score_auction(
    config: Config,
    auction_id: str,
    fates: Sequence[Fate],
    winner: AgentKind | None,
    fab: FabricationRegister,
    rng: Random,
    observed_at: datetime | None = None,
) -> OutcomeRow:
    """Score one auction through the real reward path.

    Calls ``reward.observer.score`` rather than summing terms here, so the three-state rule,
    the unknown-term guard and the completeness flag all behave exactly as they will in
    production. The only difference between this and a live observation is where the booleans
    came from — which is the difference this whole package exists to make visible.
    """
    obs, no_mortality = observations(fates, winner, fab, rng)
    return score(
        config,
        auction_id=auction_id,
        observations=obs,
        observed_at=observed_at,
        mortality_observed=no_mortality,
        mortality_source=f"simulated:{fab.version}",
    )
