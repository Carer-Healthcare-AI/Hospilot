"""App-side adapter to the RL diagnostic-machine allocation engine (Hospilot/RL).

Sibling of ``rl_gateway`` and written to the same rules: the engine is consumed as a BLACK BOX
over its HTTP API in ``mode: advisory``, supplying the world inline in the POST body. The
engine reads none of our DB.

It reuses ``rl_gateway.config`` (one engine URL, one key) and ``rl_gateway.db`` (one tenant
DSN resolver). It does not reuse ``rl_gateway.cases``/``queries``, because the bidder rules
differ: ``icu`` is directly eligible on every modality here, so the F-12 ``icu -> ward``
substitution the bed gateway performs must NOT be copied.

Modules:
    cases.json  — modality catalog: keyword -> modality, and who bids
    queries     — loader over cases.json
    client      — httpx client for /diagnostic/*
    assemble    — Hasura + machine state -> the /diagnostic/auction body
    persist     — response -> allocation.diagnostic_* rows
    trigger     — imaging contention detection -> open an auction

Install: copy this package next to ``rl_gateway`` in ``agentic-framework/`` and apply
``095_diagnostic_allocation_tables.sql`` on the tenant. Nothing here runs until an app-side
caller invokes ``trigger.on_imaging_order``.
"""

__all__ = ["assemble", "client", "persist", "queries", "trigger"]
