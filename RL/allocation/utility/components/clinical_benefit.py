"""Clinical Benefit — cap 60. D.1.

``0.30 DeteriorationRisk + 0.25 UnitBenefit + 0.20 OxygenSeverity + 0.15 OrganRisk
  + 0.10 Age/Comorbidity``

Two of the five factors have no source in the schema:

* **Target-unit benefit (.25)** — the only thing separating *sick* from *helped by the bed on
  offer*. Served by an unsigned clinician rule table until B.10 exists, and B.10 needs records
  of patients who were denied a bed, which nothing stores. Without it a 60-point component
  rewards severity alone, and the system will sometimes prefer the patient the unit cannot
  help. It is scored **per resource type**, because across a bed family the question inverts:
  for an ICU bed it asks whether escalation helps, for a ward bed whether a ward bed is
  *sufficient*. Only ``icu_bed`` has values; every other bed is absent and costs coverage.
* **Age / comorbidity (.10)** — ``hospilot.patients`` is (id, first_name, last_name, uhid,
  synced_at). No date of birth; ``diagnosis_code`` lives only on ``claims``, which is
  billing-time and post-hoc. Permanently absent, so this component runs at 90 % coverage on
  every patient.
"""

from __future__ import annotations

from typing import Any, Mapping

from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    Candidate,
    ComponentName,
    ComponentScore,
    FactorScore,
    FeatureSnapshot,
    Signal,
)
from allocation.features import labs as lab_features
from allocation.features import news2 as news2_features
from allocation.features import timeseries
from allocation.features.scale import clamp, deviation
from allocation.utility.engine import weighted


class ClinicalBenefit:
    name = ComponentName.CLINICAL_BENEFIT

    def __init__(self, config: Config, resource_type: str = "icu_bed") -> None:
        self._config = config
        # Which bed is being auctioned. The benefit table is per resource because the question
        # inverts across the family — see rules/unit_benefit.yaml.
        self._resource_type = resource_type

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        cfg = self._config
        w = cfg.weights("clinical_benefit")
        data = snapshot.for_candidate(candidate.candidate_id)
        now = snapshot.taken_at
        window = float(cfg.threshold("vitals", "slope_window_hours"))

        return weighted(
            [
                FactorScore("deterioration_risk", w["deterioration_risk"],
                            self._deterioration(data.vitals, now, window)),
                FactorScore("unit_benefit", w["unit_benefit"], self._unit_benefit(candidate)),
                FactorScore("oxygen_severity", w["oxygen_severity"],
                            self._oxygen_severity(data.vitals, now, window)),
                FactorScore("organ_risk", w["organ_risk"], self._organ_risk(data.labs, now)),
                FactorScore("age_comorbidity", w["age_comorbidity"], self._age()),
            ],
            source="clinical_benefit",
        )

    # -- factors -------------------------------------------------------------------

    def _deterioration(self, vitals: Any, now: Any, window: float) -> Signal:
        """``clamp(slope(NEWS2) / 4.0)``. Needs two readings; one is not a trend."""
        bands = self._config.threshold("news2_bands")
        scores = news2_features.score_series(vitals, bands, now, window)
        if len(scores) < 2:
            return Signal.absent("vitals", "fewer than two NEWS2 readings in the window")

        slope = timeseries.series_slope(
            [(s.recorded_at, s.points) for s in scores], now, window
        )
        if slope is None:
            return Signal.absent("vitals", "no NEWS2 slope available")

        cap = float(self._config.threshold("clinical_benefit", "news2_slope_cap_per_hour"))
        return Signal(clamp(slope / cap), "vitals.news2_slope")

    def _unit_benefit(self, candidate: Candidate) -> Signal:
        """``RuleTable[resource_type][condition_category, severity_band]``.

        An unmatched condition is ABSENT, not 0.0 — a patient whose condition is not in the
        table is one we cannot judge, not one the unit cannot help. A resource whose table is
        empty is absent for the same reason and more strongly: nobody has defined what benefit
        means for that bed yet, and the ICU answers cannot be reused because the question
        inverts (see ``rules/unit_benefit.yaml``).
        """
        source = f"rules.unit_benefit.{self._resource_type}"
        resources: Mapping[str, Any] = self._config.rule("unit_benefit").get("resources", {})
        table: Mapping[str, Any] = resources.get(self._resource_type, {})

        if not table:
            return Signal.absent(source, f"no benefit table for {self._resource_type}")

        entries = table.get("entries") or []
        if not entries:
            return Signal.absent(
                source,
                f"{table.get('status', 'unknown')} — benefit for {self._resource_type} is "
                f"undefined: {table.get('question', 'no question recorded')}",
            )

        if candidate.condition_category is None:
            return Signal.absent(source, "no condition_category on candidate")

        for entry in entries:
            if (
                entry["condition_category"] == candidate.condition_category
                and entry["severity_band"] == candidate.severity_band
            ):
                return Signal(
                    float(entry["score"]),
                    source,
                    f"{table.get('status', 'unknown')} — B.10 model absent",
                )

        return Signal.absent(
            source,
            f"no entry for {candidate.condition_category}/{candidate.severity_band}",
        )

    def _oxygen_severity(self, vitals: Any, now: Any, window: float) -> Signal:
        """``0.6 * clamp((96 - spo2)/11) + 0.4 * clamp((rr - 20)/10)``.

        D.1 notes the weights become 0.6/0.4/+0.2 renormalised once the supplemental-O2 flag
        exists (B.1). Until then this is the two-term form Appendix C uses.
        """
        latest = timeseries.latest(vitals, now, window)
        if latest is None or latest.spo2 is None or latest.respiratory_rate is None:
            return Signal.absent("vitals", "no spo2/respiratory_rate reading in the window")

        cfg = self._config
        spo2_def = clamp(
            (float(cfg.threshold("clinical_benefit", "spo2_normal")) - latest.spo2)
            / (
                float(cfg.threshold("clinical_benefit", "spo2_normal"))
                - float(cfg.threshold("clinical_benefit", "spo2_floor"))
            )
        )
        rr_dev = deviation(
            latest.respiratory_rate,
            float(cfg.threshold("clinical_benefit", "resp_rate_base")),
            float(cfg.threshold("clinical_benefit", "resp_rate_span")),
        )
        spo2_w = float(cfg.threshold("clinical_benefit", "oxygen_severity_spo2_weight"))
        rr_w = float(cfg.threshold("clinical_benefit", "oxygen_severity_rr_weight"))
        return Signal(clamp(spo2_w * spo2_def + rr_w * rr_dev), "vitals.spo2+respiratory_rate")

    def _organ_risk(self, labs: Any, now: Any) -> Signal:
        freshness = float(self._config.threshold("vitals", "lab_freshness_hours"))
        bands = self._config.threshold("organ_risk_bands")
        risk = lab_features.organ_risk(labs, bands, now, freshness)
        if risk.score is None:
            return Signal.absent("lab_results", "no analyte within the freshness window")
        return Signal(
            risk.score,
            "lab_results",
            f"{len(risk.present)}/4 analytes: {', '.join(risk.present)}",
        )

    def _age(self) -> Signal:
        """Permanently absent. ``hospilot.patients`` has no date of birth (D.1, B.3)."""
        return Signal.absent(
            "patients", "no DOB column; diagnosis_code exists only on claims (billing-time)"
        )
