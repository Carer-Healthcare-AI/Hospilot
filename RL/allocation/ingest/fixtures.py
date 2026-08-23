"""Appendix C, as data.

END_TO_END Appendix C publishes every raw row and every intermediate value for the three
bidders at 13:00. That makes it an **executable specification**, and this module is it: the
vitals tables from C.2/C.3/C.4, the labs, and the shared hospital state from C.1.

Two things this buys:

* The whole engine runs with no database and no dependency on the internal framework, so the
  utility layer is testable before an adapter exists.
* Any future change to a formula, a threshold or a cap is checked against numbers a clinician
  can read, rather than against whatever the code happened to produce last week.

The values here are transcribed, not invented. Where Appendix C's own arithmetic is
inconsistent with its declared inputs, the declared inputs win and the discrepancy is recorded
in ``tests/test_appendix_c.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping

from allocation.contracts import (
    AgentKind,
    CareNeed,
    Candidate,
    HospitalState,
    LabResult,
    MedicationOrder,
    PatientData,
    VitalsReading,
)

UTC = timezone.utc


def _at(hour: int, minute: int) -> datetime:
    """A timestamp on the worked example's day — 7 August 2026."""
    return datetime(2026, 8, 7, hour, minute, tzinfo=UTC)


#: The moment the auction opens. Appendix C computes every utility at this instant.
NOW = _at(13, 0)


# ---------------------------------------------------------------------------------------
# C.1 · Hospital state at 13:00 — shared by all three bidders
# ---------------------------------------------------------------------------------------
HOSPITAL_STATE = HospitalState(
    unit="icu",
    unit_total_beds=20,
    unit_occupied_beds=20,         # 20/20 = 1.000
    predicted_demand_4h=4.0,       # /icu/demand
    expected_discharges_4h=1.0,    # /discharge/volume
    boarding_count=7,              # /er/boarding
    lwbs_risk=0.42,                # /er/lwbs
    active_isolation_cases=2,      # infection_cases — 2 of 20 = 0.100
)


# ---------------------------------------------------------------------------------------
# The other five units — NOT from Appendix C
# ---------------------------------------------------------------------------------------
# Appendix C publishes one hospital state, C.1, and it describes the ICU, because ICU was the
# only auctionable bed when it was written. Five bed types now auction and each needs its own
# occupancy: `hospital_state` takes the unit precisely so an HDU auction stops reading ICU's
# 20/20.
#
# So these five are invented. That is a different act from the copied caps tables and budget
# pools (checklist C-1, C-2) — a fixture is invented by definition, and nothing here reaches
# a real allocation — but it means the module docstring's "transcribed, not invented" applies
# to `HOSPITAL_STATE` and the patients above, and not to this block.
#
# They are chosen for one property: **every occupancy is distinguishable from every other**
# (1.000 · 0.833 · 0.818 · 0.750 · 0.600 · 0.500). A test can therefore assert which unit's
# beds an auction read, which is the whole point of the wiring. Had they all been plausible
# and similar, reading the wrong unit would still look right.
#
# `boarding_count` and `lwbs_risk` are the two fields that are genuinely hospital-wide — ED
# boarding and left-without-being-seen are facts about the ED, not about the unit under
# auction — so they repeat unchanged. Everything else is per-unit by construction:
# `active_isolation_cases` is divided by *this* unit's bed count, and the two forecasts are
# this unit's demand and this unit's discharges.
_OTHER_UNIT_STATES = (
    HospitalState(
        unit="hdu", unit_total_beds=16, unit_occupied_beds=12,      # 0.750
        predicted_demand_4h=3.0, expected_discharges_4h=2.0,
        boarding_count=7, lwbs_risk=0.42, active_isolation_cases=1,
    ),
    HospitalState(
        unit="pacu", unit_total_beds=8, unit_occupied_beds=4,       # 0.500
        predicted_demand_4h=2.0, expected_discharges_4h=3.0,
        # A counted zero, not a missing count: PACU has no isolation case right now, so
        # isolation pressure is a measured 0.0 and keeps its coverage. Omitting the field
        # would mean "nobody looked" and drop the factor instead (D.0).
        boarding_count=7, lwbs_risk=0.42, active_isolation_cases=0,
    ),
    HospitalState(
        unit="resus", unit_total_beds=6, unit_occupied_beds=5,      # 0.833
        predicted_demand_4h=2.0, expected_discharges_4h=4.0,
        boarding_count=7, lwbs_risk=0.42, active_isolation_cases=1,
    ),
    HospitalState(
        unit="ed", unit_total_beds=22, unit_occupied_beds=18,       # 0.818
        predicted_demand_4h=6.0, expected_discharges_4h=5.0,
        boarding_count=7, lwbs_risk=0.42, active_isolation_cases=2,
    ),
    HospitalState(
        unit="ward", unit_total_beds=40, unit_occupied_beds=24,     # 0.600
        predicted_demand_4h=5.0, expected_discharges_4h=6.0,
        boarding_count=7, lwbs_risk=0.42, active_isolation_cases=3,
    ),
)

#: Every unit the fixture can answer for, by ``units.yaml`` name. ICU is Appendix C's C.1.
UNIT_STATES: Mapping[str, HospitalState] = {
    state.unit: state for state in (HOSPITAL_STATE, *_OTHER_UNIT_STATES)
}


# ---------------------------------------------------------------------------------------
# C.2 · ER — Patient A, 58 M, septic shock, boarding 55 min
# ---------------------------------------------------------------------------------------
ER_CANDIDATE = Candidate(
    candidate_id="ER-Patient-A",
    patient_token="tok-er-a",
    agent=AgentKind.ER,
    visit_id="visit-er-a",
    arrived_at=NOW - timedelta(minutes=55),   # 12:05 — DelayFactor 1.229
    current_unit="resus",
    condition_category="septic_shock",
    severity_band="pressors_reversible_source",
    needs=frozenset(
        {
            CareNeed.VENTILATION,
            CareNeed.VASOPRESSORS,
            CareNeed.ONE_TO_ONE_NURSING,
            CareNeed.CONTINUOUS_MONITORING,
        }
    ),
)

ER_VITALS = (
    # on_oxygen is inferred from an active O2 order (B.1 workaround, C.2's own note).
    VitalsReading(_at(11, 0), temperature=38.2, pulse=96, bp_systolic=104, spo2=95,
                  respiratory_rate=22, gcs=15, is_critical=False, on_oxygen=True),
    VitalsReading(_at(12, 0), temperature=38.4, pulse=108, bp_systolic=94, spo2=94,
                  respiratory_rate=24, gcs=15, is_critical=True, on_oxygen=True),
    VitalsReading(_at(12, 55), temperature=38.6, pulse=118, bp_systolic=86, spo2=93,
                  respiratory_rate=26, gcs=14, is_critical=True, on_oxygen=True),
)

ER_LABS = (
    LabResult("lactate", 4.8, _at(12, 30), unit="mmol/L", flag="H"),
    LabResult("creatinine", 2.4, _at(12, 30), unit="mg/dL", flag="H"),
    LabResult("bilirubin", 1.8, _at(12, 30), unit="mg/dL"),
    LabResult("platelets", 88, _at(12, 30), unit="10^3/uL", flag="L"),
)

ER_ORDERS = (
    MedicationOrder("Noradrenaline", "norepinephrine", "IV", "active", _at(11, 45)),
    MedicationOrder("Oxygen", "oxygen", "inhalation", "active", _at(10, 50)),
)


# ---------------------------------------------------------------------------------------
# C.3 · OT — Patient B, 65 M, cardiac bypass, intraoperative
# ---------------------------------------------------------------------------------------
OT_CANDIDATE = Candidate(
    candidate_id="OT-Patient-B",
    patient_token="tok-ot-b",
    agent=AgentKind.OT,
    admission_id="adm-ot-b",
    arrived_at=NOW - timedelta(minutes=10),   # DelayFactor 1.042
    current_unit="theatre",
    condition_category="post_op_cardiac",
    severity_band="routine",
    needs=frozenset(
        {CareNeed.VENTILATION, CareNeed.ONE_TO_ONE_NURSING, CareNeed.CONTINUOUS_MONITORING}
    ),
)

OT_VITALS = (
    # Anaesthetised: consciousness is not assessable, so gcs is absent rather than 15.
    # NEWS2 = 2 at both readings — ventilated, so the oxygen parameter alone scores.
    VitalsReading(_at(11, 30), temperature=36.2, pulse=72, bp_systolic=112, spo2=99,
                  respiratory_rate=12, gcs=None, on_oxygen=True),
    VitalsReading(_at(12, 50), temperature=36.4, pulse=78, bp_systolic=118, spo2=98,
                  respiratory_rate=14, gcs=None, on_oxygen=True),
)

OT_LABS = (
    LabResult("lactate", 1.4, _at(12, 20), unit="mmol/L"),
    LabResult("creatinine", 1.0, _at(12, 20), unit="mg/dL"),
    LabResult("bilirubin", 0.9, _at(12, 20), unit="mg/dL"),
    LabResult("platelets", 210, _at(12, 20), unit="10^3/uL"),
)


# ---------------------------------------------------------------------------------------
# C.4 · Ward — Patient C, 72 F, pneumonia, NEWS2 rising
# ---------------------------------------------------------------------------------------
WARD_CANDIDATE = Candidate(
    candidate_id="Ward-Patient-C",
    patient_token="tok-ward-c",
    agent=AgentKind.WARD,
    admission_id="adm-ward-c",
    arrived_at=NOW - timedelta(minutes=80),   # escalation requested 11:40 — DelayFactor 1.333
    current_unit="ward",
    condition_category="pneumonia",
    severity_band="progressive",
    needs=frozenset({CareNeed.CONTINUOUS_MONITORING}),
)

WARD_VITALS = (
    # C.4: "the 08:00 reading falls outside the 4-hour window" — omitted deliberately.
    VitalsReading(_at(10, 30), temperature=38.0, pulse=98, bp_systolic=112, spo2=93,
                  respiratory_rate=22, gcs=15, on_oxygen=True),
    VitalsReading(_at(12, 50), temperature=38.2, pulse=104, bp_systolic=108, spo2=92,
                  respiratory_rate=24, gcs=15, on_oxygen=True),
)

WARD_LABS = (
    LabResult("lactate", 2.2, _at(11, 15), unit="mmol/L"),
    LabResult("creatinine", 1.4, _at(11, 15), unit="mg/dL"),
    LabResult("bilirubin", 0.8, _at(11, 15), unit="mg/dL"),
    LabResult("platelets", 165, _at(11, 15), unit="10^3/uL"),
)


# ---------------------------------------------------------------------------------------
# Assembled patient data
# ---------------------------------------------------------------------------------------
PATIENT_DATA: Mapping[str, PatientData] = {
    ER_CANDIDATE.candidate_id: PatientData(
        candidate=ER_CANDIDATE,
        vitals=ER_VITALS,
        labs=ER_LABS,
        orders=ER_ORDERS,
        pending_nursing_tasks=14,          # C.2: 14 pending / 6 nurses
        ward_nurses=6,
        expected_los_days=3.5,
        icu_day_rate=18000.0,              # medical ICU day rate
        best_alternative_unit="ED Resus",
    ),
    OT_CANDIDATE.candidate_id: PatientData(
        candidate=OT_CANDIDATE,
        vitals=OT_VITALS,
        labs=OT_LABS,
        pending_nursing_tasks=6,           # C.3 states the score (0.150), not the counts
        ward_nurses=5,
        ot_cases_at_risk=3,                # C.3: "3 following cases at risk"
        expected_los_days=1.0,
        icu_day_rate=340000.0,             # cardiac package rate, not a day rate
        best_alternative_unit="PACU",
        pacu_capacity_probability=0.35,    # F-05: no source in ot_room_status
    ),
    WARD_CANDIDATE.candidate_id: PatientData(
        candidate=WARD_CANDIDATE,
        vitals=WARD_VITALS,
        labs=WARD_LABS,
        pending_nursing_tasks=22,          # C.4: 22 tasks / 4 nurses
        ward_nurses=4,
        expected_los_days=2.5,
        icu_day_rate=15000.0,
        best_alternative_unit="HDU",
    ),
}

CANDIDATES = (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE)


class FixtureDataSource:
    """A :class:`~allocation.contracts.DataSource` serving Appendix C.

    The Hasura implementation replaces this without touching anything above ``ingest/`` —
    that is the point of the protocol.

    ``hospital`` takes either a mapping of unit name to state, or a single
    :class:`~allocation.contracts.HospitalState` describing a one-unit hospital. The
    single-state form needs no unit argument because the state carries its own ``unit``, so
    a caller passing ``replace(HOSPITAL_STATE, unit_occupied_beds=14)`` gets a source that
    answers for ``icu`` and for nothing else — which is exactly what it described.
    """

    def __init__(
        self,
        hospital: HospitalState | Mapping[str, HospitalState] = UNIT_STATES,
        patients: Mapping[str, PatientData] = PATIENT_DATA,
    ) -> None:
        self._hospitals: Mapping[str, HospitalState] = (
            {hospital.unit: hospital}
            if isinstance(hospital, HospitalState)
            else dict(hospital)
        )
        self._patients = patients

    async def hospital_state(self, unit: str, at: datetime) -> HospitalState:
        # ValueError, not KeyError, for two reasons. It is the mismatch between a question and
        # a world rather than a failed container lookup — the caller described the ICU and
        # asked about the ward — and the API turns ValueError into a 400 carrying this text,
        # where a KeyError escapes as a 500 and renders its message inside quotes.
        try:
            return self._hospitals[unit]
        except KeyError as exc:
            raise ValueError(
                f"no fixture hospital state for unit {unit!r}; this source describes "
                f"{sorted(self._hospitals)}. Serving another unit's beds instead would "
                "reprice the whole auction — occupancy sets the reserve price, contention, "
                "scarcity and every budget factor. Describe the unit, or auction a bed in "
                "one of the units described."
            ) from exc

    async def patient_data(self, candidate: Candidate, at: datetime) -> PatientData:
        try:
            return self._patients[candidate.candidate_id]
        except KeyError as exc:
            raise KeyError(f"no fixture data for candidate {candidate.candidate_id!r}") from exc
