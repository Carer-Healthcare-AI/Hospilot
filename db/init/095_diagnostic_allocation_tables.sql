-- 095 · Diagnostic-machine allocation tables
--
-- Adds to the existing `allocation` schema created by 091. Nothing in `hospilot.*` and
-- nothing in the bed tables is altered by this file.
--
-- SEPARATE TABLES RATHER THAN A `resource_type` ON allocation.auction. The bed tables are
-- shaped around a bed: `resource_id`, `predicted_free_at`, one winner holding one bed. A
-- diagnostic auction grants an *interval* on a machine, against a deadline, and the columns
-- that matter (machine window, setup/cleanup, the granted start and end) have no bed
-- equivalent. Widening the bed table would mean half its columns null on every diagnostic row
-- and the other half null on every bed row, and one `mode` enum meaning two different things.
--
-- THE SAME NON-NEGOTIABLE AS 091: one row per agent PER ROUND, including losers and
-- withdrawals. Store only the winner and these stay permanently blocked, and none of it can
-- be backfilled:
--
--   denial cohort      needs requests that were REFUSED capacity
--   urgency fitting    needs the deadline and the delay actually incurred
--   fairness           needs win/loss history weighted by utility forgone
--   cap fitting        needs contested cases with per-component values

BEGIN;

CREATE SCHEMA IF NOT EXISTS allocation;


-- ---------------------------------------------------------------------------------------
-- diagnostic_auction · one row per contested capacity interval
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS allocation.diagnostic_auction (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Derived from machine + time bucket, so two orders arriving a second apart cannot open
    -- two auctions for the same scanner. The gateway also locks per (org, modality); the
    -- partial unique index below is the database-level backstop for that. Uniqueness is scoped
    -- to `live` runs (see the index) so re-running a demo/advisory scenario never collides,
    -- exactly as 091 does for beds.
    auction_key             text        NOT NULL,

    modality                text        NOT NULL,   -- ct | mri | x_ray | ultrasound
    machine_id              text        NOT NULL,

    -- live | simulation | advisory | replay. Only `live` holds machine time. Over HTTP the
    -- engine refuses `live`, so every row written by the gateway is advisory — and that has
    -- to stay visible afterwards, or advisory runs become indistinguishable from real ones.
    mode                    text        NOT NULL DEFAULT 'advisory',
    trigger_source          text        NOT NULL,

    opened_at               timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,

    max_rounds              smallint,
    rounds_run              smallint    NOT NULL DEFAULT 0,
    reserve_price           numeric(8,3),
    contention              numeric(8,3),

    winning_agent           text,
    winning_request_id      text,
    winning_bid             numeric(8,3),
    outcome                 text,                   -- awarded | no_award | aborted

    -- Caps and budgets are denominated in utility points, and each modality has its own
    -- tables, so a score is only re-derivable against the exact versions that produced it.
    caps_version            text        NOT NULL,
    config_version          text        NOT NULL,

    unsigned_rules          jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- {agent: request_id} for every agent in the opening round. An agent that was eligible
    -- and lost is the denial cohort, and without this column it leaves no trace.
    participants            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now()
);

-- Idempotency: one LIVE auction per scanner per time bucket. Simulation and advisory runs are
-- deliberately exempt so demos and testing never collide with a real auction — the same rule,
-- and the same rationale, as 091's auction_key_live_uniq for beds. Every row the gateway writes
-- over HTTP is advisory (the engine refuses `live`), so those are intentionally not deduplicated
-- at the DB level; the per-(org, modality) lock in the gateway is the primary guard, and this
-- index protects live rows once the family is ever run in that mode.
CREATE UNIQUE INDEX IF NOT EXISTS diagnostic_auction_key_live_uniq
    ON allocation.diagnostic_auction (auction_key)
    WHERE mode = 'live';

CREATE INDEX IF NOT EXISTS diagnostic_auction_modality_idx
    ON allocation.diagnostic_auction (modality, opened_at DESC);
CREATE INDEX IF NOT EXISTS diagnostic_auction_machine_idx
    ON allocation.diagnostic_auction (machine_id, opened_at DESC);


-- ---------------------------------------------------------------------------------------
-- diagnostic_bid · one row per agent PER ROUND — losers and withdrawals included
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS allocation.diagnostic_bid (
    auction_id              uuid        NOT NULL
        REFERENCES allocation.diagnostic_auction (id) ON DELETE CASCADE,
    round_index             smallint    NOT NULL,
    agent                   text        NOT NULL,
    request_id              text,

    action                  text        NOT NULL,   -- increase_bid | hold | withdraw
    -- What the agent does if it does not win: use_alternative, await_next_capacity,
    -- re_enter_later, withdraw_unplanned. Null while it is still competing.
    pathway                 text,

    amount                  numeric(8,3),
    utility                 numeric(8,3),
    ceiling                 numeric(8,3),
    alpha                   numeric(6,4),
    remaining_budget        numeric(10,3),
    -- Which limit truncated the bid, if any: the budget, the ceiling, or the leader. A bid
    -- that was clamped is not evidence of what the agent valued the scan at.
    clamped_by              text,

    created_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (auction_id, round_index, agent)
);


-- ---------------------------------------------------------------------------------------
-- diagnostic_award · the granted interval
-- ---------------------------------------------------------------------------------------
-- starts_at/ends_at INCLUDE setup and cleanup: a 15-minute procedure on a machine with 3+3
-- occupies 21 minutes. Anything reading this table to block the machine must use these
-- columns, not the procedure duration.
CREATE TABLE IF NOT EXISTS allocation.diagnostic_award (
    auction_id              uuid        PRIMARY KEY
        REFERENCES allocation.diagnostic_auction (id) ON DELETE CASCADE,
    machine_id              text        NOT NULL,
    request_id              text        NOT NULL,
    starts_at               timestamptz NOT NULL,
    ends_at                 timestamptz NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT diagnostic_award_interval_ordered CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS diagnostic_award_machine_window_idx
    ON allocation.diagnostic_award (machine_id, starts_at, ends_at);


-- ---------------------------------------------------------------------------------------
-- diagnostic_outcome · did the scan happen, and was it in time
-- ---------------------------------------------------------------------------------------
-- Written by a reward loop, not by the auction. Nothing populates this yet — the diagnostic
-- family has the same gap the bed family has, where `start_reward_loop` is never started from
-- main.py. The table exists so that turning the loop on is a code change, not a migration.
CREATE TABLE IF NOT EXISTS allocation.diagnostic_outcome (
    auction_id              uuid        PRIMARY KEY
        REFERENCES allocation.diagnostic_auction (id) ON DELETE CASCADE,
    request_id              text        NOT NULL,
    performed               boolean,
    performed_at            timestamptz,
    -- Measured against the request's latest_useful_at, not against the award: a scan that
    -- started on time and finished after the deadline still answered the question late.
    within_deadline         boolean,
    delay_minutes           integer,
    observed_at             timestamptz NOT NULL DEFAULT now()
);

COMMIT;
