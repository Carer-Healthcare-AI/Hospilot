-- 093 · Forecast output retention
--
-- ⚠ SHIP THIS WITH 091, NOT AFTER IT.
--
-- The budget Demand factor is:
--
--     Demand_i = clamp( forecast_i(this shift) / median_30( forecast_i ) , 0.8 , 1.3 )
--
-- Numerator and denominator both come from /icu/demand, so any bias in the forecast cancels
-- instead of compounding. (Historical ICU admissions must NOT be used as the denominator:
-- they count requests that were GRANTED and silently drop those denied, which is biased per
-- department — a department that loses often looks like it demands less, gets a smaller
-- budget, and loses more. Self-reinforcing.)
--
-- The 30-day median CANNOT BE BUILT RETROACTIVELY. Forecasts are computed and discarded
-- today, so if this table starts late, Demand is pinned at its 1.00 fallback for thirty days
-- after launch and the shift-recompute loop does nothing per-department in that window.
-- The clock starts the day this runs. It costs nothing to start it early.
--
-- Consequence worth knowing up front: with Demand at 1.00 and Fairness at 1.00 (no auction
-- log yet), the only factor that moves during the first ~30 days is Scarcity — which is
-- global, so relative budgets between departments will not change at all. That is the design
-- working, not a bug.

BEGIN;

CREATE SCHEMA IF NOT EXISTS allocation;

CREATE TABLE IF NOT EXISTS allocation.forecast_history (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    endpoint        text        NOT NULL,     -- /icu/demand, /icu/occupancy, /discharge/volume, ...
    scope           text        NOT NULL,     -- department, ward, or 'hospital'
    horizon         text        NOT NULL,     -- '4h', '24h', ...

    forecast_for    timestamptz NOT NULL,     -- the period forecast
    computed_at     timestamptz NOT NULL DEFAULT now(),

    value           numeric(12,4) NOT NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- request sent, for reproducibility
    raw_response    jsonb,

    -- Filled in later, so forecast quality can be measured rather than assumed.
    actual_value    numeric(12,4),
    actual_recorded_at timestamptz
);

CREATE INDEX IF NOT EXISTS forecast_history_median_idx
    ON allocation.forecast_history (endpoint, scope, horizon, computed_at DESC);

CREATE INDEX IF NOT EXISTS forecast_history_for_idx
    ON allocation.forecast_history (forecast_for);

-- The exact read behind the Demand denominator.
CREATE OR REPLACE VIEW allocation.forecast_median_30d AS
SELECT endpoint,
       scope,
       horizon,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS median_value,
       count(*)                                            AS sample_count,
       min(computed_at)                                    AS window_start
FROM allocation.forecast_history
WHERE computed_at >= now() - interval '30 days'
GROUP BY endpoint, scope, horizon;

COMMIT;
