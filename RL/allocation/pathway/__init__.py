"""What an agent does *instead of* winning. RL-Steps' closing table.

Three of the five valued actions leave the auction, and each leaves it for a different reason:

    WITHDRAW_ALTERNATIVE   there is another acceptable bed          alternatives.py
    AWAIT_NEXT_RESOURCE    another bed of this type is coming       forecast.py
    RE_ENTER_LATER         leave now, come back if things change    reentry.py

**This package exists so the auction layer never answers those questions itself.** Whether HDU
is open, when the next ICU bed lands, and whether a patient has deteriorated enough to re-enter
are all reads of the world, and the auction already took its read — ``FeatureSnapshot`` is one
immutable view per round precisely so two consumers cannot disagree about occupancy. A policy
that checked HDU for itself would be a second, unsynchronised read, and it could contradict the
Alternative Availability component that scored the same fact into the utility.

So the flow is one-directional::

    ingest  ->  FeatureSnapshot  ->  pathway.options.build()  ->  PathwayOptions  ->  policy

The policy chooses among what it is handed. It never looks anything up.

**One thing here is fabricated and says so.** :mod:`forecast` predicts the next bed release,
and no forecast endpoint supplies that today — ``expected_discharges_4h`` is a rate over four
hours, not a timed release with a confidence. RL-Steps' own example ("an ICU discharge in 35
minutes with 88 % confidence") assumes a model that does not exist. The module derives an ETA
from the rate under a stated assumption and marks every value it returns, rather than
presenting a guess as a forecast.
"""

from allocation.pathway.alternatives import available_alternatives, unit_reader
from allocation.pathway.forecast import NextRelease, next_release
from allocation.pathway.options import RoundOptions, build_options
from allocation.pathway.reentry import ReentryRegistry, ReentryCheck

__all__ = [
    "available_alternatives",
    "unit_reader",
    "NextRelease",
    "next_release",
    "RoundOptions",
    "build_options",
    "ReentryRegistry",
    "ReentryCheck",
]
