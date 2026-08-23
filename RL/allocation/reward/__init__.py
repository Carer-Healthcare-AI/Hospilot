"""The reward — what the hospital observed after the allocation.

*"The auction result itself does not tell RL whether its policy was good."*

The best-supplied part of the framework and the most fragile at the same time: eight of the
nine section 23 terms map to live tables, and the ninth decides the sign of the episode.

    transferred_to_icu     +50   ipd_admissions
    patient_stabilised     +40   vitals
    boarding_reduced       +15   visits
    cubicle_released       +10   beds
    no_mortality           +30   NOTHING  <- F-01
    safely_held            +10   ot_room_status
    second_bed_opened      +15   beds
    surgery_not_cancelled  +20   ot_surgery_schedule
    no_staffing_violation  +10   staff_roster

Until a disposition field exists, every episode is incomplete and
:func:`~allocation.reward.episode.trainable` returns nothing. That is the honest state, and
imputing the missing term would make the policy optimistic about precisely the outcome it
exists to avoid.
"""

from allocation.reward.episode import Episode, Step, build_episode, step_from, trainable
from allocation.reward.observer import (
    ObservationSource,
    PendingObservation,
    due,
    pending_for,
    score,
)
from allocation.reward.terms import (
    RewardTerm,
    discount_gamma,
    horizon_hours,
    load_terms,
    maximum_reward,
    minimum_reward,
    unobservable,
)

__all__ = [
    "Episode",
    "ObservationSource",
    "PendingObservation",
    "RewardTerm",
    "Step",
    "build_episode",
    "discount_gamma",
    "due",
    "horizon_hours",
    "load_terms",
    "maximum_reward",
    "minimum_reward",
    "pending_for",
    "score",
    "step_from",
    "trainable",
    "unobservable",
]
