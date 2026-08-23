"""App-side adapter to the RL bed-allocation auction engine (API-HUB-Backend/RL).

The engine is consumed as a BLACK BOX — we never modify or import its mutable internals.
For advisory running we drive it over its HTTP API in `mode: advisory`, supplying the world
(hospital state + candidates) inline in the POST body; the engine reads none of our DB. This
package does the reading (via db.hasura + util.forecast_client), assembles the request,
calls the engine, and persists the response into the per-tenant `allocation.*` schema.

Modules:
    config    — engine base URL / key, the §0 agent map
    assemble  — Hasura + forecast -> /auction request body      (tasks 2, 3)
    mapping   — ICU->ward agent map + /use-cases eligibility pre-check
    client    — httpx client to the engine
    persist   — response -> allocation.* rows
    trigger   — bed-release detection -> open auction           (task 8)
    reward    — pending_observation poll -> score -> outcome    (task 9)
"""
