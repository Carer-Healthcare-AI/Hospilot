"""Synthetic patients, and the latent state that drives them.

**One latent variable, everything else derived from it.** Each patient carries a severity in
``[0, 1]``; vitals, labs and care needs are all rendered from it. The alternative — sampling
each vital independently — produces patients whose respiratory rate says they are dying and
whose SpO2 says they are fine, and NEWS2 would average that into a plausible-looking middle. A
policy trained on such patients learns to read noise.

The rendering direction matters and is easy to get backwards. The engine computes
``vitals -> NEWS2 -> utility``. So the simulator must go ``severity -> vitals`` and then let the
real scorer run. Generating a NEWS2 directly and back-filling vitals to match would bypass the
feature layer entirely, and the thing under test would stop being the thing that runs.

**Absence is modelled, not avoided.** Real records have gaps, and this system's single most
repeated invariant is that a missing input is dropped rather than scored zero. A simulator that
emits complete records for every patient would never exercise the coverage machinery, and the
first real patient with no lab results would meet code that had never run. ``on_oxygen`` is
always ``None`` — that is B.1, the column does not exist in the schema, and pretending the
simulator has it would train a policy on an input production cannot supply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from random import Random

from allocation.contracts import (
    AgentKind,
    Candidate,
    CareNeed,
    LabResult,
    MedicationOrder,
    PatientData,
    VitalsReading,
)
from allocation.sim.fabricated import FabricationRegister

#: Which units each department's patients could be held in instead of ICU. Drawn from the care
#: ladder in ``rules/units.yaml``: HDU and PACU sit below ICU, ward below both. OT patients go
#: to PACU (recovery), which is what ``ot_room_status`` would describe if it modelled PACU at
#: all (F-05); ER's fallback is a resus bay; a ward patient's is HDU.
_ALTERNATIVES: dict[AgentKind, tuple[str, ...]] = {
    AgentKind.ER: ("Resus Bay 2", "HDU Bed 4"),
    AgentKind.OT: ("PACU Bay 1", "HDU Bed 4"),
    AgentKind.WARD: ("HDU Bed 4", "Ward 7"),
}

_CURRENT_UNIT: dict[AgentKind, str] = {
    AgentKind.ER: "ed",
    AgentKind.OT: "pacu",
    AgentKind.WARD: "ward",
}

_CONDITIONS: dict[AgentKind, str] = {
    AgentKind.ER: "septic_shock",
    AgentKind.OT: "post_operative_cardiac",
    AgentKind.WARD: "respiratory_deterioration",
}


@dataclass(frozen=True, slots=True)
class SimPatient:
    """A patient and their latent trajectory.

    ``severity`` is the only state. ``drift`` is this patient's own deterioration speed, drawn
    once — patients differ in how fast they fall, and a single shared rate would make the future
    deterministic and waiting risk-free, which removes the entire tension the auction exists to
    resolve.
    """

    candidate: Candidate
    severity: float
    drift: float
    arrived_at: datetime
    #: Set when the patient is placed. Drives recovery instead of deterioration, and records
    #: which unit for the outcome model — ICU and HDU do not buy the same thing.
    placed_unit: str | None = None
    placed_at: datetime | None = None
    #: Severity at the moment of placement, so the outcome model can score the counterfactual
    #: against where the patient actually started rather than against a population mean.
    severity_at_placement: float | None = None
    seed: int = 0

    @property
    def placed(self) -> bool:
        return self.placed_unit is not None

    def advanced(self, hours: float, fab: FabricationRegister, rng: Random) -> "SimPatient":
        """Move the latent state forward.

        Three regimes, and the ratios between them are the value of an ICU bed in this world:
        an unplaced patient drifts up at their own rate, a patient in a lesser unit improves
        slowly, one in ICU improves fast. Noise is added in both directions so that placement
        is not a guarantee and waiting is not a certainty — a deterministic world would let a
        policy learn an exact threshold that exists nowhere else.
        """
        if hours <= 0:
            return self

        if self.placed_unit is None:
            rate = self.drift
        elif self.placed_unit == "icu":
            rate = -fab["severity.icu_recovery_per_hour"]
        else:
            rate = -fab["severity.alternative_recovery_per_hour"]

        noise = rng.gauss(0.0, fab["severity.drift_sd_per_hour"] * math.sqrt(hours))
        return replace(self, severity=_clip(self.severity + rate * hours + noise))

    def placed_in(self, unit: str, at: datetime) -> "SimPatient":
        return replace(
            self,
            placed_unit=unit,
            placed_at=at,
            severity_at_placement=self.severity,
        )


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------------------


def make_patient(
    rng: Random,
    fab: FabricationRegister,
    agent: AgentKind,
    index: int,
    now: datetime,
) -> SimPatient:
    """One new patient for ``agent``.

    Severity is truncated-normal rather than uniform because a uniform population has as many
    dying patients as stable ones, and contention between two equally critical patients is the
    rare case, not the typical one. The auction has to be worth running most of the time and
    genuinely hard occasionally.
    """
    severity = _clip(
        rng.gauss(fab["severity.initial_mean"], fab["severity.initial_sd"]), 0.05, 0.98
    )
    drift = max(
        0.0, rng.gauss(fab["severity.drift_per_hour"], fab["severity.drift_sd_per_hour"])
    )

    candidate = Candidate(
        candidate_id=f"{agent.value}-{index:04d}",
        patient_token=f"sim-{agent.value}-{index:04d}",
        agent=agent,
        admission_id=f"adm-{index:05d}",
        visit_id=f"vis-{index:05d}",
        arrived_at=now,
        current_unit=_CURRENT_UNIT[agent],
        condition_category=_CONDITIONS[agent],
        severity_band=_band(severity),
        needs=_needs(severity, agent),
        department_id=agent.value,
    )
    return SimPatient(
        candidate=candidate,
        severity=severity,
        drift=drift,
        arrived_at=now,
        seed=rng.randrange(2**31),
    )


def _band(severity: float) -> str:
    if severity >= 0.75:
        return "critical"
    if severity >= 0.50:
        return "progressive"
    return "stable"


def _needs(severity: float, agent: AgentKind) -> frozenset[CareNeed]:
    """Care needs escalate with severity, and gate which alternatives are usable.

    This is the coupling that makes ``Q(Withdraw + Alternative)`` a real decision rather than a
    free option: HDU provides vasopressors and monitoring but not ventilation, so a patient who
    deteriorates into needing ventilation loses HDU as an alternative. The action becomes
    unavailable exactly when the patient most needs the bed — which is the clinical reality the
    capability vectors in ``rules/units.yaml`` encode.
    """
    needs = {CareNeed.CONTINUOUS_MONITORING}
    if severity >= 0.45:
        needs.add(CareNeed.VASOPRESSORS)
    if severity >= 0.70:
        needs.add(CareNeed.VENTILATION)
    if severity >= 0.85 or agent is AgentKind.OT:
        needs.add(CareNeed.ONE_TO_ONE_NURSING)
    return frozenset(needs)


# ---------------------------------------------------------------------------------------
# Rendering latent state into records the real feature layer can read
# ---------------------------------------------------------------------------------------


def render(
    patient: SimPatient,
    now: datetime,
    rng: Random,
    fab: FabricationRegister,
    history_hours: float = 4.0,
    readings: int = 4,
) -> PatientData:
    """Render ``patient`` as the rows ingest would have fetched at ``now``.

    A *series* of vitals, not one reading, because Clinical Benefit and Waiting both score the
    NEWS2 **slope** — a single row makes the deterioration factors unscoreable and silently
    drops the largest positive term. The series is back-cast from the current severity using the
    patient's own drift, so the slope the engine measures is the slope the simulator actually
    applied.
    """
    vitals = tuple(
        _reading(patient, now - timedelta(hours=history_hours * (readings - 1 - i) / max(1, readings - 1)),
                 _back_cast(patient, history_hours * (readings - 1 - i) / max(1, readings - 1)), rng)
        for i in range(readings)
    )

    return PatientData(
        candidate=patient.candidate,
        vitals=vitals,
        labs=_labs(patient, now, rng),
        orders=_orders(patient, now),
        pending_nursing_tasks=rng.randint(0, 6),
        ward_nurses=rng.randint(3, 8),
        ot_cases_at_risk=rng.randint(0, 3) if patient.candidate.agent is AgentKind.OT else None,
        expected_los_days=round(2.0 + patient.severity * 6.0, 1),
        icu_day_rate=1800.0,
        alternative_units=_ALTERNATIVES[patient.candidate.agent],
        pacu_capacity_probability=(
            round(_clip(rng.gauss(0.55, 0.2)), 2)
            if patient.candidate.agent is AgentKind.OT
            else None
        ),
    )


def _back_cast(patient: SimPatient, hours_ago: float) -> float:
    """Severity ``hours_ago``, reversing whichever regime the patient was in."""
    if patient.placed_unit is None:
        return _clip(patient.severity - patient.drift * hours_ago)
    return _clip(patient.severity + 0.09 * hours_ago)


def _reading(
    patient: SimPatient, at: datetime, severity: float, rng: Random
) -> VitalsReading:
    """Vitals from latent severity, on the live column names.

    Every parameter moves monotonically with severity and carries independent measurement
    noise, so NEWS2 rises with severity without being a deterministic function of it.

    ``on_oxygen`` is always ``None``. It is B.1 — the column does not exist in ``hospilot.vitals``
    — and it is worth 2 of NEWS2's 20 points plus two separate utility factors. Emitting it here
    would train a policy on an input production cannot supply, and would hide the coverage
    penalty every real patient will carry.
    """
    s = severity
    jitter = rng.gauss

    return VitalsReading(
        recorded_at=at,
        temperature=round(_clip(jitter(36.8 + s * 1.8, 0.3), 33.0, 41.0), 1),
        pulse=round(_clip(jitter(72 + s * 62, 6), 35, 190)),
        bp_systolic=round(_clip(jitter(128 - s * 46, 8), 55, 200)),
        bp_diastolic=round(_clip(jitter(78 - s * 22, 6), 30, 120)),
        spo2=round(_clip(jitter(98 - s * 13, 1.6), 60, 100)),
        respiratory_rate=round(_clip(jitter(15 + s * 17, 2), 6, 55)),
        gcs=round(_clip(jitter(15 - s * 5.5, 0.8), 3, 15)),
        is_critical=s >= 0.75,
        on_oxygen=None,  # B.1 — no column exists; absent, never assumed
    )


def _labs(patient: SimPatient, now: datetime, rng: Random) -> tuple[LabResult, ...]:
    """Lactate and creatinine, sometimes absent.

    Roughly one patient in five has no lactate. That is not decoration: it exercises the
    coverage path, and a simulator that always reports every test would let a policy assume an
    input that is frequently missing in practice.
    """
    s = patient.severity
    out: list[LabResult] = []
    if rng.random() > 0.20:
        out.append(
            LabResult(
                test_name="lactate",
                result_value=round(_clip(rng.gauss(1.0 + s * 5.5, 0.7), 0.3, 15.0), 1),
                reported_at=now - timedelta(minutes=rng.randint(20, 180)),
                unit="mmol/L",
                flag="high" if s > 0.55 else None,
            )
        )
    if rng.random() > 0.35:
        out.append(
            LabResult(
                test_name="creatinine",
                result_value=round(_clip(rng.gauss(80 + s * 190, 25), 30, 700)),
                reported_at=now - timedelta(minutes=rng.randint(30, 400)),
                unit="umol/L",
            )
        )
    return tuple(out)


def _orders(patient: SimPatient, now: datetime) -> tuple[MedicationOrder, ...]:
    """Vasopressor orders, matched against the drug-class rule table by generic name.

    Emitted as ``generic_name`` rather than as a flag because that is what
    ``hospilot.pharmacy_orders`` holds — the schema has no drug classification, so the system
    matches strings against ``rules/drug_classes.yaml`` (F-14). Emitting a pre-classified flag
    would skip the one step that can go wrong in production.
    """
    if CareNeed.VASOPRESSORS not in patient.candidate.needs:
        return ()
    return (
        MedicationOrder(
            medication_name="Noradrenaline 4mg/50ml",
            generic_name="norepinephrine",
            route="IV",
            status="active",
            prescribed_at=now - timedelta(minutes=45),
            dispensed_at=now - timedelta(minutes=30),
        ),
    )
