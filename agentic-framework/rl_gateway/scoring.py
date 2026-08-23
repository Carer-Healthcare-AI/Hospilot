"""Reward scoring — a decoupled reimplementation of the engine's reward.observer.score.

The engine treats scoring as the backend's job (ObservationSource has no implementation), so we
own it here rather than importing RL internals. The rule is the engine's exactly: award a
term's points when observed True; 0 when False; EXCLUDE when None (unknown) and mark the
episode incomplete. A term is only "missing" if it was passed in as None — a term that never
applied to this auction is not an observation gap.

⚠ Keep REWARD_POINTS in sync with API-HUB-Backend/RL/allocation/config/reward.yaml. These
values are RL-Steps' own and UNFITTED — they define what any future policy optimises for.
Mortality (no_mortality) is handled separately and is always unknown today (F-01).
"""

from __future__ import annotations

MORTALITY_TERM = "no_mortality"
HORIZON_HOURS = 4.0

# name -> points, from reward.yaml (won: positive after ICU win; lost: the counterfactual).
REWARD_POINTS: dict[str, int] = {
    "transferred_to_icu": 50,
    "patient_stabilised": 40,
    "boarding_reduced": 15,
    "cubicle_released": 10,
    "no_mortality": 30,          # F-01: no structured source, always unknown
    "safely_held": 10,
    "second_bed_opened": 15,
    "surgery_not_cancelled": 20,
    "no_staffing_violation": 10,
    "patient_deterioration": -60,
    "additional_boarding": -20,
    "emergency_escalation": -20,
    "ot_throughput": 25,
    "revenue": 10,
}


def score(
    observations: dict[str, bool | None],
    mortality_observed: bool | None = None,
) -> dict:
    """observations: {term: True|False|None}. Returns the auction_outcome payload."""
    unknown = sorted(set(observations) - set(REWARD_POINTS))
    if unknown:
        # A misspelt term would silently remove points from the objective (engine parity).
        raise KeyError(f"observations for terms not in REWARD_POINTS: {unknown}")

    awarded: dict[str, int] = {}
    missing: list[str] = []
    for name, points in REWARD_POINTS.items():
        if name == MORTALITY_TERM:
            continue
        seen = observations.get(name)
        if seen is None:
            if name in observations:
                missing.append(name)
            continue
        if seen:
            awarded[name] = points

    if mortality_observed is None:
        missing.append(MORTALITY_TERM)
    elif mortality_observed:
        awarded[MORTALITY_TERM] = REWARD_POINTS[MORTALITY_TERM]

    return {
        "terms": awarded,
        "reward_total": sum(awarded.values()),
        "mortality_observed": mortality_observed,
        "complete": not missing,
        "missing_terms": sorted(set(missing)),
    }
