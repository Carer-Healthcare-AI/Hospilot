"""Resolve a list of queries and report what each one selects, or why it refuses.

    python try_queries.py                 # the built-in spread
    python try_queries.py "your sentence" # one of your own
"""
import sys

from allocation import profiles  # noqa: F401 — registers the family
from allocation.trigger.query import UnknownUseCase, matched_tokens, resolve_profile

DEFAULTS = [
    # the founding sentence — two units named, one is a bidder
    "ER, OT, and ICU/Ward demand compete for one limited ICU bed",
    "one limited ICU bed",
    "a ward bed is free",
    "an HDU bed has opened up",
    "recovery bay free in PACU",
    "resus bay available",
    "ED bed free",
    # phrasings that name a unit and a bed less formally
    "ITU bed free",
    "high dependency bed available",
    "step-down bed on the ward",
    "bed free in intensive care",
    # no unit named at all
    "who should get the next available bed?",
    "a bed is free",
    # genuinely two resources
    "we have an ICU bed and a ward bed free",
    # not a bed at all
    "a ventilator has become available",
    "",
]


def main(queries):
    width = max(len(q) for q in queries) + 2
    for q in queries:
        label = repr(q) if q else "'' (empty)"
        try:
            profile = resolve_profile(q)
        except UnknownUseCase as exc:
            print(f"{label:<{width}} REFUSED  {str(exc).split('.')[0]}")
        else:
            how = matched_tokens(q, profile)
            print(f"{label:<{width}} -> {profile.resource_type.value:<10} via {how}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULTS)
