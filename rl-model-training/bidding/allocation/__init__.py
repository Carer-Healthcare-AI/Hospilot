"""HOSPILOT allocation auction.

Implements the framework specified in ``RL-Steps.md``, ``RL_STEPS_END_TO_END.md`` and
``AGENT_BUDGET.md``. Those documents are normative: no component, cap, weight, formula or
mechanism is changed here. See ``BUILD_SPEC.md`` for the input inventory and the assumption
register, and its section 9 for open flags.

Layering — a module may import downward only::

    profiles/   resource-type configuration (the multi-use-case seam)
    config/     caps, thresholds, rule tables, versioning
        |
    ingest/     raw rows in, no scoring, no judgement
        |
    features/   raw rows -> normalised signals in [0, 1]
        |
    utility/    weights, caps, coverage renormalisation, assembly
        |
    budget/     accrual, contention, spend, renewal
        |
    auction/    round state machine, reserve, settlement
        |
    policy/     emits alpha and an action; heuristic first, RL behind the same interface
        |
    reward/     deferred outcome observation
        |
    audit/      the log that every blocked model in BUILD_SPEC section 6.2 waits on

``contracts`` sits outside the stack: it holds the frozen types that cross these boundaries
and imports nothing from the package.
"""

__version__ = "0.1.0"
