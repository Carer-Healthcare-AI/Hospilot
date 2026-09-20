"""Deterministic diagnostic scenarios — the heuristic baseline, before any RL.

**These come first on purpose.** RL_EXPERIMENTS_LOG's whole history is of learned policies
evaluated against a benchmark that was not yet trustworthy, and the conclusion each time was
that the benchmark had to be fixed before the compute was worth spending. A diagnostic RL run
launched before there is a heuristic ladder to beat, on scenarios nobody has read the output
of, would repeat that exactly.

Nothing here samples. Every arrival time, deadline and yield is written down, so two runs of
one scenario produce identical numbers and a change in the numbers means a change in the code.
That is what makes these usable as regression fixtures as well as as a baseline.

Every clinical value below is INVENTED — a plausible shape, not a governed number. The yields
in particular (``modality_yields``) are the framework's §13 structure filled in by hand: they
express *this question is answered by CT and not by MRI* without claiming the specific 0.9 is
right.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine.budgets import (
    diagnostic_shift,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.config import for_modality
from allocation.use_cases.diagnostic_machine.contracts import (
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticRequest,
    MachineStatus,
)
from allocation.use_cases.diagnostic_machine.profiles import MODALITIES, ModalityProfile
from allocation.use_cases.diagnostic_machine.scoring import DiagnosticContext
from allocation.use_cases.diagnostic_machine.simulator import (
    Arrival,
    SimulationResult,
    simulate,
)

#: Every scenario opens here. A fixed instant, so a deadline in a fixture is a real time and
#: not a duration from "now" that moves whenever the tests run.
EPOCH = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One reproducible day on one modality."""

    name: str
    description: str
    modality: DiagnosticModality
    machines: tuple[DiagnosticMachineState, ...]
    arrivals: tuple[Arrival, ...]
    hours: float = 4.0
    alternative_machines: tuple[DiagnosticMachineState, ...] = ()
    #: What the scenario is built to demonstrate. Read by the tests, and by anyone deciding
    #: whether a run's numbers are the ones this scenario is supposed to produce.
    expects: str = ""

    @property
    def profile(self) -> ModalityProfile:
        return MODALITIES.get(self.modality)

    def run(self, config: Config) -> SimulationResult:
        """Run this scenario against the heuristic policy."""
        scoped = for_modality(config, self.modality)
        shift = diagnostic_shift(EPOCH, hours=max(self.hours, 8.0))
        budgets = open_diagnostic_budgets(
            scoped, self.profile, shift, machines=self.machines
        )
        return simulate(
            scoped,
            self.profile,
            self.machines,
            self.arrivals,
            budgets,
            starts_at=EPOCH,
            ends_at=EPOCH + timedelta(hours=self.hours),
            alternative_machines=self.alternative_machines,
        )


# -- fixture builders ----------------------------------------------------------------------


def machine(
    machine_id: str,
    modality: DiagnosticModality,
    capabilities: Sequence[str],
    hours: float = 8.0,
    status: MachineStatus = MachineStatus.AVAILABLE,
    setup_minutes: int | None = None,
    cleanup_minutes: int | None = None,
) -> DiagnosticMachineState:
    """A machine open from :data:`EPOCH`, with its modality's setup and cleanup by default."""
    profile = MODALITIES.get(modality)
    return DiagnosticMachineState(
        machine_id=machine_id,
        modality=modality,
        status=status,
        window_starts_at=EPOCH,
        window_ends_at=EPOCH + timedelta(hours=hours),
        capabilities=frozenset(capabilities),
        setup_minutes=profile.setup_minutes if setup_minutes is None else setup_minutes,
        cleanup_minutes=profile.cleanup_minutes if cleanup_minutes is None else cleanup_minutes,
    )


def request(
    request_id: str,
    agent: AgentKind,
    question: str,
    modality: DiagnosticModality,
    at_minutes: float,
    useful_for_minutes: float,
    yields: Mapping[DiagnosticModality, float],
    capabilities: Sequence[str] = (),
    duration_minutes: float | None = None,
    transport_minutes: int = 15,
    impact_probability: float = 0.7,
    impact_importance: float = 0.8,
    operational_impact: float | None = None,
    reschedulable: bool = False,
) -> Arrival:
    """One request arriving ``at_minutes`` after :data:`EPOCH`."""
    profile = MODALITIES.get(modality)
    arrived = EPOCH + timedelta(minutes=at_minutes)
    duration = (
        profile.typical_duration
        if duration_minutes is None
        else timedelta(minutes=duration_minutes)
    )
    context = (
        None
        if operational_impact is None
        else DiagnosticContext(operational_impact=operational_impact)
    )
    return Arrival(
        at=arrived,
        context=context,
        request=DiagnosticRequest(
            request_id=request_id,
            patient_token=f"patient-{request_id}",
            agent=agent,
            clinical_question=question,
            requested_procedure=f"{modality.value} {question}",
            eligible_modalities=frozenset(yields),
            requested_at=arrived,
            latest_useful_at=arrived + timedelta(minutes=useful_for_minutes),
            estimated_duration=duration,
            diagnostic_yield=yields[modality],
            management_impact_probability=impact_probability,
            management_impact_importance=impact_importance,
            required_capabilities=frozenset(capabilities),
            transport_minutes=transport_minutes,
            modality_yields=dict(yields),
            reschedulable=reschedulable,
        ),
    )


# -- the scenarios ---------------------------------------------------------------------------

CT = DiagnosticModality.CT
MRI = DiagnosticModality.MRI
US = DiagnosticModality.ULTRASOUND
XR = DiagnosticModality.X_RAY


def three_way_contention() -> Scenario:
    """One CT, three departments, one moment. The founding shape of the use case.

    ER's question is the most time-critical and the best answered by CT, so it should win the
    first auction. ICU and OT should not be *abandoned* — the scanner frees up well inside
    their windows, so the honest exit is to wait for scheduled capacity, and they should be
    served in later rounds of the day.
    """
    return Scenario(
        name="three_way_contention",
        description="ER, ICU and OT contest a single CT scanner",
        modality=CT,
        machines=(machine("CT-01", CT, ("head", "chest", "abdomen", "angiography")),),
        arrivals=(
            request("er-1", AgentKind.ER, "intracranial haemorrhage", CT,
                    at_minutes=0, useful_for_minutes=45,
                    yields={CT: 0.95, MRI: 0.35}, capabilities=("head",)),
            request("icu-1", AgentKind.ICU, "ventilated, worsening infiltrates", CT,
                    at_minutes=0, useful_for_minutes=120,
                    yields={CT: 0.85, MRI: 0.20}, capabilities=("chest",)),
            request("ot-1", AgentKind.OT, "pre-operative staging", CT,
                    at_minutes=0, useful_for_minutes=210,
                    yields={CT: 0.70, MRI: 0.65}, capabilities=("abdomen",)),
        ),
        expects="ER wins first; ICU and OT wait for capacity rather than abandoning",
    )


def four_way_contention_with_appointments() -> Scenario:
    """``three_way_contention`` plus a 4th bidder: scheduled/outpatient CT demand.

    agent-extension (2026-08-27). ER/ICU/OT arrivals are byte-for-byte identical to
    ``three_way_contention`` above, so that scenario's own behaviour is unaffected by this
    one existing — this is a NEW scenario, not a change to the original. APPOINTMENTS is
    the one new request: declared reschedulable, a longer safe-delay window (genuinely
    outpatient, not urgent), and the lowest diagnostic yield of the four — it should
    plausibly lose the opening round to the more urgent requests and be served later,
    exactly the same "not abandoned, served later" shape the founding scenario already
    demonstrates for ICU and OT.
    """
    return Scenario(
        name="four_way_contention_with_appointments",
        description="ER, ICU, OT and a scheduled outpatient CT appointment contest one scanner",
        modality=CT,
        machines=(machine("CT-01", CT, ("head", "chest", "abdomen", "angiography")),),
        arrivals=(
            request("er-1", AgentKind.ER, "intracranial haemorrhage", CT,
                    at_minutes=0, useful_for_minutes=45,
                    yields={CT: 0.95, MRI: 0.35}, capabilities=("head",)),
            request("icu-1", AgentKind.ICU, "ventilated, worsening infiltrates", CT,
                    at_minutes=0, useful_for_minutes=120,
                    yields={CT: 0.85, MRI: 0.20}, capabilities=("chest",)),
            request("ot-1", AgentKind.OT, "pre-operative staging", CT,
                    at_minutes=0, useful_for_minutes=210,
                    yields={CT: 0.70, MRI: 0.65}, capabilities=("abdomen",)),
            request("appt-1", AgentKind.APPOINTMENTS, "routine outpatient surveillance", CT,
                    at_minutes=0, useful_for_minutes=240,
                    yields={CT: 0.55, MRI: 0.50}, capabilities=("abdomen",),
                    reschedulable=True),
        ),
        expects=(
            "ER wins first; ICU, OT and the appointment all wait for capacity rather than "
            "being abandoned — the appointment is not a co-winner of the same auction as "
            "whichever request wins first, it is served afterward in its own later slot"
        ),
    )


def alternative_answers_the_question() -> Scenario:
    """The §13 case: a free ultrasound answers one question and not the other.

    Both requests want CT. Only one of them has a question ultrasound can answer, and that one
    should divert; the other should not, however contested the scanner gets. A ladder-based
    design cannot express this — it would rank ultrasound below CT for everyone.
    """
    return Scenario(
        name="alternative_answers_the_question",
        description="A free ultrasound is a real substitute for one question, not the other",
        modality=CT,
        machines=(machine("CT-01", CT, ("head", "abdomen")),),
        arrivals=(
            request("er-head", AgentKind.ER, "head injury, GCS dropping", CT,
                    at_minutes=0, useful_for_minutes=40,
                    yields={CT: 0.95, US: 0.05}, capabilities=("head",)),
            request("er-gallbladder", AgentKind.ER, "right upper quadrant pain", CT,
                    at_minutes=0, useful_for_minutes=150,
                    yields={CT: 0.70, US: 0.85}, capabilities=("abdomen",)),
            request("icu-abdo", AgentKind.ICU, "intra-abdominal sepsis source", CT,
                    at_minutes=0, useful_for_minutes=180,
                    yields={CT: 0.90, US: 0.40}, capabilities=("abdomen",)),
        ),
        alternative_machines=(machine("US-01", US, ("abdomen", "vascular")),),
        expects="er-gallbladder diverts to ultrasound; the head injury never does",
    )


def deadline_pressure() -> Scenario:
    """More short-deadline demand than one scanner can serve.

    The mechanism cannot make capacity appear, so some requests must expire. What it must NOT
    do is expire the most valuable ones — and it must record expiry as expiry rather than
    quietly counting an unserved request as served.
    """
    return Scenario(
        name="deadline_pressure",
        description="Six urgent CT requests, one scanner, forty-minute windows",
        modality=CT,
        machines=(machine("CT-01", CT, ("head", "chest")),),
        hours=2.0,
        arrivals=tuple(
            request(
                f"{agent.value}-{index}", agent, f"acute question {index}", CT,
                at_minutes=index * 4, useful_for_minutes=40,
                yields={CT: 0.6 + 0.1 * (index % 4)}, capabilities=("head",),
            )
            for index, agent in enumerate(
                (AgentKind.ER, AgentKind.ICU, AgentKind.OT,
                 AgentKind.ER, AgentKind.ICU, AgentKind.OT)
            )
        ),
        expects="capacity binds; unserved requests are recorded as expired, never as answered",
    )


def mri_scarcity() -> Scenario:
    """MRI's real shape: 45-minute studies, one machine, a six-hour horizon.

    The same code path as CT with different data — which is the claim the whole family rests
    on. If this needs a branch anywhere, the abstraction has failed.
    """
    return Scenario(
        name="mri_scarcity",
        description="One MRI, 45-minute studies, three departments over four hours",
        modality=MRI,
        machines=(machine("MRI-01", MRI, ("brain", "spine", "cardiac")),),
        arrivals=(
            request("er-cord", AgentKind.ER, "suspected cord compression", MRI,
                    at_minutes=0, useful_for_minutes=180,
                    yields={MRI: 0.95, CT: 0.30}, capabilities=("spine",)),
            request("icu-brain", AgentKind.ICU, "unexplained coma", MRI,
                    at_minutes=10, useful_for_minutes=240,
                    yields={MRI: 0.85, CT: 0.45}, capabilities=("brain",)),
            request("ot-cardiac", AgentKind.OT, "pre-operative cardiac assessment", MRI,
                    at_minutes=20, useful_for_minutes=300,
                    yields={MRI: 0.80, CT: 0.55}, capabilities=("cardiac",)),
        ),
        expects="one 45-minute study at a time; bookings never overlap",
    )


def capability_is_a_hard_filter() -> Scenario:
    """A machine that cannot do the procedure is not capacity, at any price.

    Two CTs, one without angiography. The angiography request must be booked on CT-02 or not
    at all — never on CT-01 with a penalty applied, which is what would let a learned policy
    buy its way past a safety constraint.
    """
    return Scenario(
        name="capability_is_a_hard_filter",
        description="Two CTs, one lacking angiography; the CTPA can only go to one of them",
        modality=CT,
        machines=(
            machine("CT-01", CT, ("head", "chest")),
            machine("CT-02", CT, ("head", "chest", "angiography", "contrast")),
        ),
        arrivals=(
            request("er-pe", AgentKind.ER, "suspected pulmonary embolism", CT,
                    at_minutes=0, useful_for_minutes=90,
                    yields={CT: 0.95}, capabilities=("angiography", "contrast")),
            request("icu-head", AgentKind.ICU, "post-arrest imaging", CT,
                    at_minutes=0, useful_for_minutes=120,
                    yields={CT: 0.80}, capabilities=("head",)),
        ),
        expects="the CTPA lands on CT-02; nothing is booked onto a machine that cannot do it",
    )


def portable_costs_no_transport() -> Scenario:
    """X-ray at the bedside. Transport burden must be zero however far away the patient is.

    The same request scored against a portable and a non-portable modality should differ by
    exactly the transport component, which is what makes ``portable`` a profile field rather
    than a comment.
    """
    return Scenario(
        name="portable_costs_no_transport",
        description="A portable X-ray unit, with a patient an hour of transport away",
        modality=XR,
        machines=(machine("XR-01", XR, ("chest", "portable")),),
        arrivals=(
            request("icu-chest", AgentKind.ICU, "line position check", XR,
                    at_minutes=0, useful_for_minutes=120,
                    yields={XR: 0.75}, capabilities=("chest",), transport_minutes=60),
            request("er-chest", AgentKind.ER, "suspected pneumothorax", XR,
                    at_minutes=0, useful_for_minutes=60,
                    yields={XR: 0.85}, capabilities=("chest",), transport_minutes=5),
        ),
        expects="transport burden scores 0 for both; distance does not move a portable bid",
    )



def degraded_fleet() -> Scenario:
    """One of two scanners is down, and the requests can feel it.

    Exists because ``resource_stress`` was a DEAD ENCODER COLUMN until it did: with every
    machine healthy in every scenario, the component scored a constant 0.0 and the learner was
    paying for a feature that carried no information. That is the exact defect the bed encoder
    shipped with, caught here by the sweep in ``test_diagnostic_encoder.py``.

    It is also a real operational state — a scanner in unplanned maintenance during a busy
    shift — and the mechanism should bid harder when the fleet is degraded.
    """
    return Scenario(
        name="degraded_fleet",
        description="Two CTs, one out of service; contention on what is left",
        modality=CT,
        machines=(
            machine("CT-01", CT, ("head", "chest", "abdomen")),
            machine("CT-02", CT, ("head", "chest", "abdomen"),
                    status=MachineStatus.OUT_OF_SERVICE),
        ),
        arrivals=(
            request("er-1", AgentKind.ER, "acute stroke window", CT,
                    at_minutes=0, useful_for_minutes=45,
                    yields={CT: 0.95}, capabilities=("head",)),
            request("icu-1", AgentKind.ICU, "ventilator-associated pneumonia", CT,
                    at_minutes=5, useful_for_minutes=150,
                    yields={CT: 0.80}, capabilities=("chest",)),
            request("ot-1", AgentKind.OT, "post-operative collection", CT,
                    at_minutes=10, useful_for_minutes=200,
                    yields={CT: 0.75}, capabilities=("abdomen",)),
        ),
        expects="resource stress is non-zero; the healthy scanner takes every booking",
    )


def operational_pressure() -> Scenario:
    """Answers that unblock downstream flow, and answers that do not.

    Exists because ``operational_impact`` was the other DEAD COLUMN: no scenario supplied the
    context, so every request scored the neutral 0.5 fallback and the component could never
    distinguish anything. Here OT's answer releases a theatre list and ER's releases nothing,
    which is the difference the component exists to express.
    """
    return Scenario(
        name="operational_pressure",
        description="A scan that unblocks a theatre list against one that unblocks nothing",
        modality=CT,
        machines=(machine("CT-01", CT, ("head", "abdomen")),),
        arrivals=(
            request("ot-list", AgentKind.OT, "is theatre safe to proceed", CT,
                    at_minutes=0, useful_for_minutes=180,
                    yields={CT: 0.80}, capabilities=("abdomen",),
                    operational_impact=0.95),
            request("er-minor", AgentKind.ER, "low-suspicion head injury", CT,
                    at_minutes=0, useful_for_minutes=200,
                    yields={CT: 0.80}, capabilities=("head",),
                    operational_impact=0.05),
            request("icu-routine", AgentKind.ICU, "routine surveillance", CT,
                    at_minutes=30, useful_for_minutes=220,
                    yields={CT: 0.75}, capabilities=("abdomen",),
                    operational_impact=0.30),
        ),
        expects="operational impact separates the requests; OT's theatre-blocking scan wins",
    )


#: Every scenario, by name. Deterministic and cheap — the whole set runs in well under a
#: second, which is what lets it be a test fixture rather than a nightly job.
SCENARIOS: Mapping[str, Callable[[], Scenario]] = {
    "three_way_contention": three_way_contention,
    "four_way_contention_with_appointments": four_way_contention_with_appointments,
    "alternative_answers_the_question": alternative_answers_the_question,
    "deadline_pressure": deadline_pressure,
    "mri_scarcity": mri_scarcity,
    "capability_is_a_hard_filter": capability_is_a_hard_filter,
    "portable_costs_no_transport": portable_costs_no_transport,
    "degraded_fleet": degraded_fleet,
    "operational_pressure": operational_pressure,
}


def all_scenarios() -> tuple[Scenario, ...]:
    return tuple(build() for build in SCENARIOS.values())


def run_all(config: Config) -> dict[str, SimulationResult]:
    """Run every scenario. The heuristic baseline, in one call."""
    return {scenario.name: scenario.run(config) for scenario in all_scenarios()}


__all__ = [
    "EPOCH",
    "SCENARIOS",
    "Scenario",
    "all_scenarios",
    "machine",
    "request",
    "run_all",
]
