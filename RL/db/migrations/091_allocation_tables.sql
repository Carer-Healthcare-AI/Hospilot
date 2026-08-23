-- 091 · Allocation auction tables
--
-- Creates a SEPARATE `allocation` schema. Nothing in `hospilot.*` is altered by this file;
-- the one change to a hospilot table is isolated in 092 and needs its own sign-off.
--
-- WHY THE SHAPE MATTERS MORE THAN USUAL. Four things in the framework are blocked on data
-- that only running the system produces, and none of it can be backfilled:
--
--   B.10  Expected ICU benefit    needs patients who were DENIED a bed
--   B.11  Criticality             needs request timestamps
--   B.12  Fairness v2/v3          needs win/loss history, weighted by utility forgone
--   B.13  Cap fitting             needs contested cases with per-component values
--
-- Store only the winner, or only the utility total, and all four stay blocked permanently.
-- Hence: one row per agent PER ROUND including losers and withdrawals, with the full
-- component breakdown and the coverage fraction attached.

BEGIN;

CREATE SCHEMA IF NOT EXISTS allocation;


-- ---------------------------------------------------------------------------------------
-- auction · one row per contested resource release
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS allocation.auction (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Derived from resource + release-time bucket so a re-firing discharge prediction
    -- cannot open two auctions on one bed (END_TO_END section 7).
    auction_key             text        NOT NULL,

    resource_type           text        NOT NULL,
    resource_id             text        NOT NULL,

    -- live | simulation | advisory | replay.
    -- Only `live` holds a bed, decrements a real budget, and is valid RL training data.
    -- Without this column, hand-fired test runs are indistinguishable from real
    -- allocations afterwards and B.10/B.13 would train on auctions where no bed was held.
    mode                    text        NOT NULL DEFAULT 'live',
    trigger_source          text        NOT NULL,

    predicted_free_at       timestamptz NOT NULL,
    opened_at               timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,

    max_rounds              smallint    NOT NULL,
    rounds_run              smallint    NOT NULL DEFAULT 0,
    reserve_price           numeric(8,3),

    winning_agent           text,
    winning_candidate_id    text,
    winning_bid             numeric(8,3),
    outcome                 text,                  -- awarded | no_award | aborted

    -- A stored row that cannot be re-derived is useless to B.13. Budgets are denominated in
    -- utility points, so a caps change re-derives every budget in the system.
    caps_version            text        NOT NULL,
    config_version          text        NOT NULL,

    -- Rule tables still on assumed values when this auction ran, as {table: status}.
    unsigned_rules          jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Every eligible bidder and the patient it bid for, as {agent: candidate_id}.
    -- B.10 needs patients who were DENIED a bed. A candidate that was eligible but never
    -- bid is a denial, and without this column it leaves no trace anywhere.
    participants            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT auction_mode_valid
        CHECK (mode IN ('live', 'simulation', 'advisory', 'replay')),
    CONSTRAINT auction_outcome_valid
        CHECK (outcome IS NULL OR outcome IN ('awarded', 'no_award', 'aborted'))
);

-- Idempotency: one LIVE auction per resource per release bucket. Simulation and advisory
-- runs are deliberately exempt so testing never collides with a real auction.
CREATE UNIQUE INDEX IF NOT EXISTS auction_key_live_uniq
    ON allocation.auction (auction_key)
    WHERE mode = 'live';

CREATE INDEX IF NOT EXISTS auction_opened_idx    ON allocation.auction (opened_at DESC);
CREATE INDEX IF NOT EXISTS auction_resource_idx  ON allocation.auction (resource_type, resource_id);
CREATE INDEX IF NOT EXISTS auction_training_idx  ON allocation.auction (mode, closed_at)
    WHERE mode = 'live';


-- ---------------------------------------------------------------------------------------
-- auction_bid · one row per agent PER ROUND — including losers and withdrawals
-- ---------------------------------------------------------------------------------------
-- "Both episodes are needed, which is why the log must record the losers' bids and
--  utilities, not only the winner's." — END_TO_END sections 23-24
CREATE TABLE IF NOT EXISTS allocation.auction_bid (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auction_id              uuid        NOT NULL REFERENCES allocation.auction (id) ON DELETE CASCADE,
    round_index             smallint    NOT NULL,

    agent                   text        NOT NULL,
    candidate_id            text        NOT NULL,
    patient_token           text,

    action                  text        NOT NULL,   -- withdraw | hold | increase_bid
    amount                  numeric(8,3) NOT NULL,

    -- Bid != utility != ceiling. ER wins at 118 while valuing the bed at 171 (section 18).
    utility                 numeric(8,3) NOT NULL,
    ceiling                 numeric(8,3) NOT NULL,
    alpha                   numeric(5,4),           -- Increment = alpha * (ceiling - bid)

    contention              numeric(5,3),
    outcome_factor          numeric(4,3),
    cost                    numeric(8,3),

    -- The eight components as scored, plus per-component coverage. B.13 cap fitting needs
    -- these on contested cases; Fairness v3 needs the utility forgone by losers.
    component_points        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    component_coverage      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    policy_name             text,                   -- heuristic | rl:<version>
    decided_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bid_action_valid
        CHECK (action IN ('withdraw', 'hold', 'increase_bid')),
    CONSTRAINT bid_within_ceiling
        CHECK (action = 'withdraw' OR amount <= ceiling)
);

CREATE UNIQUE INDEX IF NOT EXISTS auction_bid_uniq
    ON allocation.auction_bid (auction_id, round_index, agent);
CREATE INDEX IF NOT EXISTS auction_bid_agent_idx ON allocation.auction_bid (agent, decided_at DESC);


-- ---------------------------------------------------------------------------------------
-- agent_budget · one row per agent per shift
-- ---------------------------------------------------------------------------------------
-- All four factors are stored, not just the product. "A budget of 80 with no record of which
-- factor moved it is unauditable, and re-deriving it after a cap change is impossible."
-- — AGENT_BUDGET section 10
CREATE TABLE IF NOT EXISTS allocation.agent_budget (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent                   text        NOT NULL,
    shift_id                text        NOT NULL,
    shift_start             timestamptz NOT NULL,
    shift_end               timestamptz NOT NULL,

    -- B = Base * Demand * Criticality * Scarcity * Fairness    (RL-Steps section 4)
    --
    -- Base is COMMON to every department (RL-Steps line 141, "a common Base Budget = 700
    -- points for 8 hours"). All four factors are stored, never just the product: a budget
    -- with no record of which factor moved it is unauditable and cannot be re-derived after
    -- a cap change.
    base                    numeric(10,3) NOT NULL,
    demand_factor           numeric(5,3)  NOT NULL,
    -- Department-level and shift-invariant: the share of this department's ICU requests
    -- needing admission within 30 minutes (RL-Steps section 4, line 184). Hard-coded to the
    -- doc's published values until the auction log can measure it.
    criticality_factor      numeric(5,3)  NOT NULL DEFAULT 1.000,
    fairness_factor         numeric(5,3)  NOT NULL,
    scarcity_factor         numeric(5,3)  NOT NULL,   -- GLOBAL: identical for every agent

    -- Provenance per factor: computed, or fallen back, and from what.
    -- A factor of 1.000 that was measured and one that defaulted are the same number and
    -- entirely different facts. Today Demand falls back (no 30-day forecast history, F-18)
    -- and Fairness is a v1 constant (no auction log, B.12) — only Scarcity is live. Without
    -- this column a later reader cannot tell which of the four actually meant anything.
    factor_sources          jsonb         NOT NULL DEFAULT '{}'::jsonb,

    budget_total            numeric(10,3) NOT NULL,
    budget_remaining        numeric(10,3) NOT NULL,
    spent_this_shift        numeric(10,3) NOT NULL DEFAULT 0,
    recovered_this_shift    numeric(10,3) NOT NULL DEFAULT 0,

    -- seed | computed. Shift 0 seeds with Base (Demand and Fairness both 1.0). Seeding with
    -- RL-Steps' declared 1000/800/700 instead would drop ER ~12x at the first recompute.
    source                  text        NOT NULL,

    -- Inputs behind Base, so it can be re-derived when caps or targets move.
    n_win                   smallint,
    n_req                   smallint,
    cost_per_win            numeric(8,3),
    cost_per_loss           numeric(8,3),

    caps_version            text        NOT NULL,
    config_version          text        NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT budget_source_valid CHECK (source IN ('seed', 'computed')),
    CONSTRAINT budget_remaining_sane CHECK (budget_remaining <= budget_total)
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_budget_uniq
    ON allocation.agent_budget (agent, shift_id);
CREATE INDEX IF NOT EXISTS agent_budget_shift_idx ON allocation.agent_budget (shift_start DESC);


-- ---------------------------------------------------------------------------------------
-- utility_snapshot · the inputs behind each scored utility
-- ---------------------------------------------------------------------------------------
-- One immutable read of the world per auction round. Every component in a round sees
-- identical data; without that, two components can read /icu/occupancy a second apart and
-- disagree, and the stored utility is not reproducible — which makes B.13 impossible.
CREATE TABLE IF NOT EXISTS allocation.utility_snapshot (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auction_id              uuid        NOT NULL REFERENCES allocation.auction (id) ON DELETE CASCADE,
    round_index             smallint    NOT NULL,
    taken_at                timestamptz NOT NULL,

    hospital_state          jsonb       NOT NULL,   -- occupancy, demand, discharges, boarding, lwbs, isolation
    patient_data            jsonb       NOT NULL,   -- per candidate: vitals, labs, orders as read
    factor_signals          jsonb       NOT NULL,   -- normalised [0,1] signals with their source

    caps_version            text        NOT NULL,
    config_version          text        NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS utility_snapshot_uniq
    ON allocation.utility_snapshot (auction_id, round_index);


-- ---------------------------------------------------------------------------------------
-- auction_outcome · the reward, observed after the fact
-- ---------------------------------------------------------------------------------------
-- "The auction result itself does not tell RL whether its policy was good. The hospital must
--  observe what happened afterward." — RL-Steps section 23
--
-- FLAG F-01: `no_mortality` has NO STRUCTURED SOURCE. There is no deceased/expired/death
-- column or enum value anywhere in the hospilot schema; discharge_summaries holds free text
-- only. It is the largest single reward term (+30 / -60) and it sets the sign of the
-- episode. Nullable until a disposition field exists — and null must NOT be read as "no
-- death occurred".
CREATE TABLE IF NOT EXISTS allocation.auction_outcome (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auction_id              uuid        NOT NULL REFERENCES allocation.auction (id) ON DELETE CASCADE,
    observed_at             timestamptz NOT NULL DEFAULT now(),
    horizon_hours           numeric(5,2) NOT NULL,

    terms                   jsonb       NOT NULL,   -- {term: points}, one per section 23 line
    reward_total            numeric(8,3) NOT NULL,

    mortality_observed      boolean,                -- NULL = unknown, NOT "no death"
    mortality_source        text,                   -- null until a disposition field exists

    complete                boolean     NOT NULL DEFAULT false,
    missing_terms           text[]      NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS auction_outcome_uniq ON allocation.auction_outcome (auction_id);

COMMIT;
