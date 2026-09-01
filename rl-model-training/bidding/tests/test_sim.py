"""The simulator, and the properties that make anything measured in it worth reporting.

Four things are being protected here, in rough order of how badly a regression would hurt:

1. **Reproducibility.** Every comparison in RL_READINESS §4.2 is paired — same seeds, same
   arrivals, same trajectories — because *"otherwise a 5 % difference is indistinguishable from
   luck"*. If the world is not deterministic given a seed, every number this project produces is
   noise, and nothing else in this file matters.
2. **The seam.** The simulator must enter through ``DataSource``, so that the utilities a policy
   trains against are computed by the real eight components rather than invented.
3. **Time passing.** A frozen patient teaches a policy that waiting is free.
4. **The fabrication register.** Every invented constant must be declared, or the falsification
   sweep cannot see it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind, DataSource, QAction
from allocation.reward.episode import trainable
from allocation.rl.encoder import ACTIONS, SIZE, StateEncoder
from allocation.rl.policy import PARAM_COUNT, LinearQPolicy, QWeights
from allocation.sim.dataset import generate
from allocation.sim.fabricated import DEFAULT, register
from allocation.sim.outcomes import observations, resolve
from allocation.sim.patients import make_patient, render
from allocation.sim.world import SimDataSource, SimWorld

UTC = timezone.utc
START = datetime(2026, 8, 7, 7, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config():
    return load_config()


# ---------------------------------------------------------------------------------------
# 1 · Reproducibility — everything else depends on this
# ---------------------------------------------------------------------------------------


def test_the_same_seed_produces_the_same_world():
    a, b = SimWorld(seed=5), SimWorld(seed=5)
    for world in (a, b):
        world.arrivals_until(START + timedelta(hours=6))
        world.advance_to(START + timedelta(hours=6))

    assert sorted(a.patients) == sorted(b.patients)
    assert [a.patients[k].severity for k in sorted(a.patients)] == [
        b.patients[k].severity for k in sorted(b.patients)
    ]


def test_different_seeds_produce_different_worlds():
    a, b = SimWorld(seed=5), SimWorld(seed=6)
    for world in (a, b):
        world.arrivals_until(START + timedelta(hours=6))
    assert sorted(a.patients) != sorted(b.patients) or [
        a.patients[k].severity for k in sorted(a.patients)
    ] != [b.patients[k].severity for k in sorted(b.patients)]


def test_bed_releases_are_reproducible():
    assert SimWorld(seed=3).release_schedule(24.0) == SimWorld(seed=3).release_schedule(24.0)


def test_a_whole_generation_run_is_reproducible(config):
    a = generate(config, seed=4, shifts=2)
    b = generate(config, seed=4, shifts=2)
    assert a.auctions == b.auctions
    assert [t.bid for t in a.transitions] == [t.bid for t in b.transitions]
    assert [t.reward for t in a.transitions] == [t.reward for t in b.transitions]


def test_patient_trajectories_do_not_depend_on_the_policy(config):
    """The property paired evaluation rests on.

    Two policies must face identical patients. If patient noise came from a stream the policy
    also drew from, a policy that asked one extra question would desynchronise the world and
    every comparison would be measuring the desynchronisation.
    """
    from allocation.policy.heuristic import HeuristicPolicy

    class ChattyPolicy(HeuristicPolicy):
        """Behaves identically but consumes extra randomness."""

        name = "chatty"

        def decide_q(self, *args, **kwargs):
            import random
            random.random()
            random.random()
            return super().decide_q(*args, **kwargs)

    plain = generate(config, seed=9, shifts=2)
    chatty = generate(config, seed=9, shifts=2, policy=ChattyPolicy(config))
    assert [t.utility for t in plain.transitions] == [t.utility for t in chatty.transitions]


# ---------------------------------------------------------------------------------------
# 2 · The seam
# ---------------------------------------------------------------------------------------


def test_the_sim_source_satisfies_the_datasource_protocol():
    """Structurally, not via isinstance — ``DataSource`` is a plain Protocol.

    Marking it ``runtime_checkable`` would only check that the method *names* exist anyway, so
    this asserts the part that actually matters: both are coroutine functions with the
    signatures the ingest layer calls.
    """
    import inspect

    source = SimDataSource(SimWorld(seed=1))
    for name in ("hospital_state", "patient_data"):
        method = getattr(source, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be awaitable"
    assert set(inspect.signature(source.hospital_state).parameters) == {"unit", "at"}
    assert set(inspect.signature(source.patient_data).parameters) == {"candidate", "at"}


def test_an_unknown_unit_raises_rather_than_substituting():
    """``DataSource.hospital_state``'s contract: a substituted unit's occupancy is worse than none."""
    source = SimDataSource(SimWorld(seed=1))
    with pytest.raises(KeyError, match="no unit"):
        asyncio.run(source.hospital_state("theatre", START))


def test_an_unknown_patient_raises_rather_than_inventing_vitals():
    from allocation.contracts import Candidate

    source = SimDataSource(SimWorld(seed=1))
    ghost = Candidate(candidate_id="nobody", patient_token="t", agent=AgentKind.ER)
    with pytest.raises(KeyError, match="no simulated patient"):
        asyncio.run(source.patient_data(ghost, START))


def test_utilities_come_from_the_real_engine(config):
    """Not invented: they must sit on the 0-200 scale the caps define, with real coverage."""
    dataset = generate(config, seed=2, shifts=2)
    assert dataset.transitions
    assert all(0.0 < t.utility <= 200.0 for t in dataset.transitions)
    assert all(t.ceiling >= t.utility - 1e-6 for t in dataset.transitions)


# ---------------------------------------------------------------------------------------
# 3 · Time passing
# ---------------------------------------------------------------------------------------


def test_an_unplaced_patient_deteriorates():
    world = SimWorld(seed=8)
    # Six hours of arrivals, not one: at 1.3 patients/h a one-hour window can draw zero, and a
    # test that silently passes on an empty population asserts nothing.
    world.arrivals_until(START + timedelta(hours=6))
    assert world.patients, "the arrival process must produce patients"
    before = {k: p.severity for k, p in world.patients.items()}
    world.advance_to(START + timedelta(hours=12))
    after = {k: p.severity for k, p in world.patients.items()}
    risen = sum(1 for k in before if after[k] > before[k])
    assert risen > len(before) / 2, "most unplaced patients should be worse after five hours"


def test_a_placed_patient_improves():
    world = SimWorld(seed=8)
    world.arrivals_until(START + timedelta(hours=6))
    world.advance_to(START + timedelta(hours=6))
    cid = max(world.patients, key=lambda k: world.patients[k].severity)
    before = world.patients[cid].severity
    world.place(cid, "icu", START + timedelta(hours=6))
    world.advance_to(START + timedelta(hours=10))
    assert world.patients[cid].severity < before


def test_icu_beats_the_alternative_which_beats_waiting():
    """The ratio between these three rates *is* the value of an ICU bed in this world."""
    assert (
        DEFAULT["severity.icu_recovery_per_hour"]
        > DEFAULT["severity.alternative_recovery_per_hour"]
        > 0.0
        > -DEFAULT["severity.drift_per_hour"]
    )


def test_placing_a_patient_holds_the_bed():
    """Occupancy must respond to allocation, or Scarcity and the reserve never move."""
    world = SimWorld(seed=8)
    world.arrivals_until(START + timedelta(hours=6))
    world.advance_to(START + timedelta(hours=6))
    assert world.patients
    before = world.occupancy["icu"]
    world.discharge("icu")
    assert world.occupancy["icu"] == before - 1
    world.place(next(iter(world.patients)), "icu", START + timedelta(hours=6))
    assert world.occupancy["icu"] == before


def test_rendered_vitals_track_latent_severity():
    from random import Random

    rng = Random(0)
    from dataclasses import replace

    base = make_patient(rng, DEFAULT, AgentKind.ER, 1, START)
    well = replace(base, severity=0.1)
    sick = replace(base, severity=0.9)

    sick_data = render(sick, START, Random(1), DEFAULT)
    well_data = render(well, START, Random(1), DEFAULT)
    assert sick_data.vitals[-1].respiratory_rate > well_data.vitals[-1].respiratory_rate
    assert sick_data.vitals[-1].spo2 < well_data.vitals[-1].spo2


def test_supplemental_oxygen_is_never_emitted():
    """B.1 — the column does not exist. Emitting it would train on an input production lacks."""
    from random import Random

    patient = make_patient(Random(0), DEFAULT, AgentKind.ER, 1, START)
    data = render(patient, START, Random(1), DEFAULT)
    assert all(v.on_oxygen is None for v in data.vitals)


# ---------------------------------------------------------------------------------------
# 4 · The fabrication register
# ---------------------------------------------------------------------------------------


def test_the_register_versions_its_values():
    assert DEFAULT.version == register().version
    assert register({"outcome.mortality_base": 0.9}).version != DEFAULT.version


def test_an_undeclared_constant_is_refused():
    """Undeclared means invisible to the sweep that detects a policy fitting it."""
    with pytest.raises(KeyError, match="unknown fabricated constants"):
        register({"outcome.made_up": 1.0})


def test_the_outcome_constants_are_identified_separately():
    """They are the objective, and the sweep only perturbs them."""
    names = {f.name for f in DEFAULT.outcome_constants}
    assert "outcome.mortality_severity_slope" in names
    assert "arrival.er_share" not in names
    assert all(f.kind == "outcome" for f in DEFAULT.outcome_constants)


def test_perturbing_changes_the_version():
    """So an episode from a perturbed world can never be pooled with a baseline one."""
    assert DEFAULT.perturbed("outcome.mortality_base", 1.5).version != DEFAULT.version


# ---------------------------------------------------------------------------------------
# The reward signal — Essential 1
# ---------------------------------------------------------------------------------------


def test_episodes_are_now_trainable(config):
    """Against the real schema ``trainable()`` returns nothing, permanently (F-01).

    In a simulated world mortality is observable, so completeness becomes achievable. Anything
    materially below 1.0 is a bug in the outcome model, not a property of the world.
    """
    dataset = generate(config, seed=3, shifts=4)
    assert dataset.episodes
    assert dataset.completeness > 0.95, dataset.summary()
    assert trainable(dataset.episodes)


def test_a_term_that_cannot_apply_does_not_make_an_episode_incomplete(config):
    """A theatre term on an auction with no surgical bidder is not an observation gap."""
    from random import Random

    world = SimWorld(seed=1)
    world.arrivals_until(START + timedelta(hours=2))
    medical = [
        p for p in world.patients.values() if p.candidate.agent is not AgentKind.OT
    ][:2]
    fates = [resolve(p, 4.0, DEFAULT, Random(0)) for p in medical]
    obs, _ = observations(fates, None, DEFAULT, Random(0))
    assert "surgery_not_cancelled" not in obs
    assert "safely_held" not in obs


def test_arranged_care_terms_are_attributed_to_the_action_that_arranged_them(config):
    """The defect the Q-space was built to fix, asserted directly.

    ``safely_held`` (+10) must follow a chosen ``WITHDRAW_ALTERNATIVE``, never merely the fact
    that a surgical patient existed.
    """
    from dataclasses import replace
    from random import Random

    world = SimWorld(seed=1)
    world.arrivals_until(START + timedelta(hours=3))
    surgical = next(p for p in world.patients.values() if p.candidate.agent is AgentKind.OT)
    fate = resolve(surgical, 4.0, DEFAULT, Random(0))

    without, _ = observations([fate], None, DEFAULT, Random(0))
    with_exit, _ = observations(
        [replace(fate, exit_action=QAction.WITHDRAW_ALTERNATIVE)], None, DEFAULT, Random(0)
    )
    assert without["safely_held"] is False
    assert with_exit["safely_held"] is True


def test_losers_are_scored_too(config):
    """§24 is not an afterthought — a reward from the winner alone cannot teach that losing was survivable."""
    dataset = generate(config, seed=5, shifts=3)
    losers = [t for t in dataset.transitions if not t.won]
    assert losers
    assert all(t.complete for t in losers)


# ---------------------------------------------------------------------------------------
# The encoder and the learned policy — Essential 3
# ---------------------------------------------------------------------------------------


def test_the_encoder_version_is_a_hash_of_the_feature_list():
    """F-24: a policy served a different vector is undefined, not degraded.

    Asserted against the hash rather than by mutating the module, so the test states the
    contract — *the version is a function of the feature names, in order* — instead of merely
    observing that a poke at global state changed something.
    """
    import hashlib

    from allocation.rl.encoder import NAMES

    expected = hashlib.sha256("|".join(NAMES).encode()).hexdigest()[:12]
    assert StateEncoder().version == expected

    reordered = hashlib.sha256("|".join(reversed(NAMES)).encode()).hexdigest()[:12]
    assert reordered != expected, "reordering the vector must change the version"


def test_every_encoded_feature_is_bounded(config):
    dataset = generate(config, seed=6, shifts=2)
    assert dataset.transitions
    for transition in dataset.transitions:
        assert len(transition.state) == SIZE
        assert all(0.0 <= x <= 1.0 for x in transition.state)


def test_absence_is_flagged_not_zeroed():
    """A policy given 0.0 for an unknown safe wait must be able to tell that apart from zero."""
    from allocation.rl.encoder import NAMES

    assert "safe_wait_known" in NAMES
    assert "release_known" in NAMES
    assert "alternative_available" in NAMES


def test_weights_round_trip_through_a_flat_vector():
    weights = QWeights.from_flat(
        [0.1 * i for i in range(PARAM_COUNT)], StateEncoder().version
    )
    assert QWeights.from_flat(weights.flat(), weights.encoder_version) == weights


def test_a_policy_refuses_to_load_against_a_different_encoding(tmp_path):
    """The check F-24 exists to demand."""
    path = QWeights.zeros("deadbeefcafe").save(tmp_path / "p.json")
    with pytest.raises(ValueError, match="Refit rather than reinterpret"):
        QWeights.load(path)


def test_the_learned_policy_only_picks_feasible_actions(config):
    """An exit it could not name a plan for would fail to construct."""
    weights = QWeights.from_flat(
        [(-1) ** i * 0.5 for i in range(PARAM_COUNT)], StateEncoder().version
    )
    dataset = generate(
        config, seed=7, shifts=3, policy=LinearQPolicy(config, weights),
    )
    assert dataset.transitions
    for transition in dataset.transitions:
        assert transition.q_action.value in transition.feasible


def test_the_learned_policy_publishes_its_q_values(config):
    """Section 12 publishes both sides — ``Q(Continue) = 41`` against ``Q(Withdraw) = 58``.

    The losing estimate is the label a value function trains on, and nothing recomputes a
    Q-value after the fact because the state it was estimated from has moved on. Asserted on
    the audit rows, which is where it has to survive.
    """
    from pathlib import Path

    from allocation.ingest.scenarios import load_scenario
    from allocation.trigger.runtime import run_allocation

    now = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)
    scenario = Path(__file__).resolve().parents[1] / "scenarios" / "ward_crash.yaml"
    source, candidates, _ = load_scenario(scenario, now)

    weights = QWeights.from_flat([0.2] * PARAM_COUNT, StateEncoder().version)
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=now,
        query="ICU bed", policy=LinearQPolicy(config, weights), read_alternatives=True,
    )

    rows = run.bundle.bids
    assert rows
    assert all(row.q_values for row in rows), "a learned policy must publish what it ranked"
    for row in rows:
        assert row.q_action in row.q_values
        assert set(row.q_values) <= set(row.feasible_actions)


def test_a_zero_policy_still_produces_valid_auctions(config):
    """The CEM start point. It must run, or generation 0 has no fitness to rank."""
    dataset = generate(
        config, seed=8, shifts=2,
        policy=LinearQPolicy(config, QWeights.zeros(StateEncoder().version)),
    )
    assert dataset.auctions > 0
    assert dataset.completeness > 0.9
