"""The simulated hospital: arrivals, occupancy, and the ``DataSource`` seam.

**It enters through the same seam the real reader will.** ``SimDataSource`` implements
``DataSource`` — ``hospital_state(unit, at)`` and ``patient_data(candidate, at)`` — so every
layer above ``ingest/`` runs unmodified: the eight components, the ceiling, the budget, the
auction, the guards, settlement and audit. The alternative, generating utilities directly, would
mean the thing being trained on was never the thing that runs.

Two properties this file exists to guarantee.

**Time actually passes.** ``patient_data(candidate, at)`` renders the patient *as they are at*
``at``, and severity advances between calls. The engine re-scores every round for exactly this
reason — RL-Steps has ER rising 135 → 148 → 171 across three rounds — and against the static
fixtures that never happened. A simulator whose patients are frozen would teach a policy that
waiting is free, which is the single most important thing it must not learn.

**Everything is seeded and reproducible.** Paired runs are the basis of every comparison in
RL_READINESS §4.2: *"Same arrival stream, same trajectories, same bed releases for every policy.
Otherwise a 5 % difference is indistinguishable from luck."* Two worlds built with the same seed
produce identical arrivals and identical trajectories regardless of which policy runs, because
patient noise is drawn from a per-patient stream keyed on the patient's own seed rather than
from a shared one that the policy's own random draws could desynchronise.

**Occupancy responds to allocation.** A won bed is held for the length of stay; ICU occupancy
falls when a patient is discharged and rises when one is placed. Without this the scarcity
factor, the reserve price and contention are all constants, and three of the four budget factors
stop moving — the mechanism would be exercised but never stressed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from random import Random
from typing import Iterator, Mapping, Sequence

from allocation.contracts import (
    AgentKind,
    Candidate,
    HospitalState,
    PatientData,
    ResourceType,
)
from allocation.sim.fabricated import DEFAULT, FabricationRegister
from allocation.sim.patients import SimPatient, make_patient, render

#: Bed counts per unit. ICU is Appendix C.1's 20; the rest are the fixture's, chosen so every
#: occupancy is distinguishable from every other and a test can prove which unit was read.
_BEDS: Mapping[str, int] = {
    "icu": 20, "hdu": 16, "pacu": 8, "resus": 6, "ed": 22, "ward": 40,
}


@dataclass
class SimWorld:
    """A hospital that advances in time.

    The world owns three independent random streams. Splitting them is not tidiness: a shared
    stream would make the arrival sequence depend on how many times the *policy* happened to
    draw, so two policies would face different patients and no paired comparison would be valid.
    """

    seed: int = 0
    fab: FabricationRegister = DEFAULT
    #: Timezone-aware by construction. ``budget/shifts.resolve_shift`` returns aware boundaries,
    #: and mixing the two raises the moment a shift rolls — an hour into the first run, far from
    #: the line that chose the start.
    start: datetime = datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc)
    #: Occupied beds per unit. ICU starts full — Appendix C.1's 20/20, the contested case.
    occupancy: dict[str, int] = field(default_factory=lambda: {
        "icu": 20, "hdu": 12, "pacu": 4, "resus": 5, "ed": 18, "ward": 24,
    })
    now: datetime = field(init=False)
    patients: dict[str, SimPatient] = field(default_factory=dict, init=False)
    #: Occupied ICU beds and when each frees, so occupancy responds to allocation.
    _holds: list[tuple[datetime, str]] = field(default_factory=list, init=False)
    _counter: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.now = self.start
        self._arrivals = Random(self.seed * 7919 + 1)
        self._releases = Random(self.seed * 7919 + 2)
        self._render = Random(self.seed * 7919 + 3)

    # -- time --------------------------------------------------------------------------

    def advance_to(self, moment: datetime) -> None:
        """Move the world to ``moment``: patients evolve, held beds free.

        Patient noise is drawn from a stream keyed on that patient's own seed and the elapsed
        step, so a patient's trajectory is a function of the world seed alone — never of how
        many other patients exist or of anything the policy did.
        """
        if moment <= self.now:
            return
        hours = (moment - self.now).total_seconds() / 3600.0

        for cid, patient in list(self.patients.items()):
            stream = Random(
                _stream_key(patient.seed, int((moment - self.start).total_seconds()) // 60)
            )
            self.patients[cid] = patient.advanced(hours, self.fab, stream)

        freed = [h for h in self._holds if h[0] <= moment]
        for _, unit in freed:
            self.occupancy[unit] = max(0, self.occupancy[unit] - 1)
        self._holds = [h for h in self._holds if h[0] > moment]

        self.now = moment

    # -- arrivals ----------------------------------------------------------------------

    def arrivals_until(self, moment: datetime) -> tuple[SimPatient, ...]:
        """New patients arriving in ``(now, moment]``, as a Poisson stream.

        Departments are sampled from the configured mix rather than round-robin, so the bidder
        set genuinely varies — sometimes two ER patients contend, sometimes ER is absent. A
        fixed rotation would give the policy a periodic signal it could exploit and that no
        hospital has.
        """
        hours = max(0.0, (moment - self.now).total_seconds() / 3600.0)
        rate = self.fab["arrival.candidate_per_hour"]
        count = _poisson(self._arrivals, rate * hours)

        out: list[SimPatient] = []
        for _ in range(count):
            self._counter += 1
            agent = self._sample_agent()
            at = self.now + timedelta(hours=self._arrivals.random() * hours)
            patient = make_patient(self._arrivals, self.fab, agent, self._counter, at)
            self.patients[patient.candidate.candidate_id] = patient
            out.append(patient)
        return tuple(out)

    def _sample_agent(self) -> AgentKind:
        roll = self._arrivals.random()
        er = self.fab["arrival.er_share"]
        ot = self.fab["arrival.ot_share"]
        if roll < er:
            return AgentKind.ER
        if roll < er + ot:
            return AgentKind.OT
        return AgentKind.WARD

    def release_schedule(self, hours: float) -> tuple[datetime, ...]:
        """Bed-release moments over the next ``hours``, as a Poisson stream.

        This is the arrival process for *supply*, and it replaces ``session.event_schedule``'s
        evenly spaced events — whose own docstring concedes it "is adequate for exercising the
        lifecycle and is not a demand model". Burn rate measured against a regular schedule
        reads differently from one measured against a bursty one, and the budget is precisely a
        pacing mechanism.
        """
        rate = self.fab["arrival.bed_release_per_hour"]
        moments: list[datetime] = []
        t = 0.0
        while True:
            gap = self._releases.expovariate(rate) if rate > 0 else math.inf
            t += gap
            if t >= hours:
                break
            moments.append(self.start + timedelta(hours=t))
        return tuple(moments)

    # -- allocation feedback -----------------------------------------------------------

    def place(self, candidate_id: str, unit: str, at: datetime) -> None:
        """Record that a patient was placed, and hold the bed for their stay.

        The hold is what closes the loop: a bed given away is unavailable until the patient
        leaves, so winning now costs capacity later. Without it occupancy is a constant and
        Scarcity, the reserve price and contention never move.
        """
        patient = self.patients.get(candidate_id)
        if patient is None:
            return
        self.patients[candidate_id] = patient.placed_in(unit, at)
        stay_hours = 18.0 + patient.severity * 48.0
        self.occupancy[unit] = min(_BEDS[unit], self.occupancy.get(unit, 0) + 1)
        self._holds.append((at + timedelta(hours=stay_hours), unit))

    def discharge(self, unit: str) -> None:
        """Free one bed in ``unit`` — what a release event physically is."""
        self.occupancy[unit] = max(0, self.occupancy.get(unit, 0) - 1)

    def active(self) -> tuple[SimPatient, ...]:
        """Patients still needing a bed, sickest first."""
        return tuple(
            sorted(
                (p for p in self.patients.values() if not p.placed),
                key=lambda p: -p.severity,
            )
        )

    def state(self, unit: str) -> HospitalState:
        """This unit's bed state, as the forecast endpoints would report it."""
        total = _BEDS[unit]
        occupied = min(total, self.occupancy.get(unit, 0))
        waiting = [p for p in self.patients.values() if not p.placed]

        return HospitalState(
            unit=unit,
            unit_total_beds=total,
            unit_occupied_beds=occupied,
            predicted_demand_4h=float(
                len([p for p in waiting if p.candidate.current_unit == unit]) + 2
            ),
            expected_discharges_4h=round(
                self.fab["arrival.bed_release_per_hour"] * 4.0, 1
            ),
            boarding_count=len([p for p in waiting if p.candidate.agent is AgentKind.ER]),
            lwbs_risk=round(min(0.9, 0.05 + 0.06 * len(waiting)), 2),
            active_isolation_cases=max(0, int(occupied * 0.1)),
        )


class SimDataSource:
    """``DataSource`` over a :class:`SimWorld`.

    The entire adapter. Everything above ``ingest/`` is unchanged, which is the property that
    makes anything measured here transferable: the utilities a policy trains against are
    computed by the same eight components, from the same NEWS2 scorer, under the same caps.
    """

    def __init__(self, world: SimWorld) -> None:
        self._world = world
        self._render = Random(world.seed * 7919 + 4)

    async def hospital_state(self, unit: str, at: datetime) -> HospitalState:
        """Raises for a unit it cannot describe, exactly as the protocol requires."""
        if unit not in _BEDS:
            raise KeyError(
                f"the simulated hospital has no unit {unit!r}. Substituting another unit's "
                "occupancy would price the auction against a different unit entirely."
            )
        self._world.advance_to(at)
        return self._world.state(unit)

    async def patient_data(self, candidate: Candidate, at: datetime) -> PatientData:
        self._world.advance_to(at)
        patient = self._world.patients.get(candidate.candidate_id)
        if patient is None:
            raise KeyError(
                f"no simulated patient {candidate.candidate_id!r}. A candidate that the world "
                "has never heard of would otherwise be scored from invented vitals."
            )
        # Keyed on the patient and the minute, so re-reading the same patient at the same
        # instant yields the same record — a snapshot must be reproducible from the log.
        stream = Random(_stream_key(patient.seed, int(at.timestamp()) // 60, "render"))
        return render(patient, at, stream, self._world.fab)


def _stream_key(*parts: object) -> int:
    """A stable integer seed from any parts.

    Python 3.14 dropped tuple seeds, and hashing the tuple with the builtin `hash` would give a
    seed that changes between processes under hash randomisation — which would quietly destroy
    the reproducibility every paired comparison depends on. A content hash does not.
    """
    payload = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _poisson(rng: Random, mean: float) -> int:
    """Knuth's algorithm. Small means only, which is all this needs."""
    if mean <= 0:
        return 0
    target = math.exp(-mean)
    count, product = 0, rng.random()
    while product > target:
        count += 1
        product *= rng.random()
    return count
