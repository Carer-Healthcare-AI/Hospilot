-- 094 · The decision behind the bid — RL-Steps' closing table.
--
-- Migration 091 recorded `action` (withdraw | hold | increase_bid), which is what happened to
-- the BID. It cannot express what happened to the PATIENT, and three strategically different
-- exits collapse into one indistinguishable 'withdraw' row:
--
--     a patient moved safely to HDU
--     a patient waiting on a bed predicted in 20 minutes
--     a patient abandoned with nothing arranged
--
-- WHY THIS BLOCKS TRAINING, not merely reporting. `config/reward.yaml` already pays
-- `safely_held` (+10, "a losing surgical case was safely held in PACU") and
-- `second_bed_opened` (+15, "a further ICU bed became available inside the window"). Those are
-- outcomes of the first two exits. With one undifferentiated withdrawal those points attach to
-- whichever agent happened to have bid, for a hand-off no policy ever chose — so the reward is
-- unattributable, and a policy trained on it learns to credit a bid for a pathway decision.
--
-- `withdraw_unplanned` is not in RL-Steps' table. It is added because that table has no entry
-- for the case that forces most real withdrawals — cannot win, no alternative open, no release
-- predicted — and because an abandonment must be prevented from collecting the terms the
-- arranged exits earn. A rising count on it is the mechanism reporting that it is rationing
-- past what the alternatives can absorb, and nothing else in the schema reports that.

BEGIN;

ALTER TABLE allocation.auction_bid
    -- Which of the six decisions produced this row. NULL for rows written before this
    -- migration: those auctions ran under a three-action policy and their withdrawals are
    -- genuinely unclassifiable. Back-filling them to 'withdraw_unplanned' would assert that
    -- nothing was arranged, which nobody knows.
    ADD COLUMN IF NOT EXISTS q_action          text,

    -- What the exit committed to: target_unit, safe_hold_minutes, expected_release_at,
    -- release_probability, and the flattened re-entry trigger. Empty object for a non-exit
    -- and for withdraw_unplanned — the two cases that arranged nothing. That emptiness is
    -- read by the reward observer, so it must not be confused with an unread plan.
    ADD COLUMN IF NOT EXISTS plan              jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Estimated value per action considered, {q_action: value}. Section 12 publishes both
    -- sides — Q(Continue) = 41 against Q(Withdraw) = 58 — and the LOSING estimate is the
    -- label a value function trains on. Storing only the argmax discards it permanently:
    -- nothing recomputes a Q-value after the fact, because the state it was estimated from
    -- has moved on. Empty for a rule-based policy, which ranks nothing and must not appear to.
    ADD COLUMN IF NOT EXISTS q_values          jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Which actions were available at all. An evaluation that cannot separate *declined* from
    -- *unavailable* reads a policy that never had an alternative as one that never wanted one.
    ADD COLUMN IF NOT EXISTS feasible_actions  text[] NOT NULL DEFAULT '{}';

ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_q_action_valid;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_q_action_valid CHECK (
        q_action IS NULL OR q_action IN (
            'win_now', 'continue',
            'withdraw_alternative', 'await_next_resource',
            're_enter_later', 'withdraw_unplanned'
        )
    );

-- The bid mechanic must follow the decision. Enforced here as well as in
-- `allocation.contracts.Decision` because the log outlives the process that wrote it: a row
-- claiming a strategic exit while recording an increased bid would be read years later as a
-- safe hand-off that also won the auction.
ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_q_action_matches_action;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_q_action_matches_action CHECK (
        q_action IS NULL
        OR (q_action IN ('win_now', 'continue') AND action IN ('increase_bid', 'hold'))
        OR (q_action IN ('withdraw_alternative', 'await_next_resource',
                         're_enter_later', 'withdraw_unplanned') AND action = 'withdraw')
    );

-- An exit that claims to have arranged something must say what. This is the '+' in
-- "Withdraw + Alternative", enforced by the table rather than by the writer's good intentions
-- — it is the constraint that stops an abandonment collecting safely_held's +10.
ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_arranged_exit_has_plan;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_arranged_exit_has_plan CHECK (
        q_action IS DISTINCT FROM 'withdraw_alternative' OR plan ? 'target_unit'
    );

ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_await_exit_has_forecast;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_await_exit_has_forecast CHECK (
        q_action IS DISTINCT FROM 'await_next_resource'
        OR (plan ? 'expected_release_at' AND plan ? 'release_probability')
    );

ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_reenter_exit_has_trigger;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_reenter_exit_has_trigger CHECK (
        q_action IS DISTINCT FROM 're_enter_later' OR plan ? 'reentry_expires_at'
    );

-- The unplanned exit arranged nothing, and must not look as though it did.
ALTER TABLE allocation.auction_bid
    DROP CONSTRAINT IF EXISTS bid_unplanned_exit_has_no_plan;

ALTER TABLE allocation.auction_bid
    ADD CONSTRAINT bid_unplanned_exit_has_no_plan CHECK (
        q_action IS DISTINCT FROM 'withdraw_unplanned' OR plan = '{}'::jsonb
    );

-- The two queries this exists to make cheap: how often each decision is taken, and how often
-- the system abandons a patient. The second is the health metric the mechanism otherwise
-- cannot report.
CREATE INDEX IF NOT EXISTS auction_bid_q_action_idx
    ON allocation.auction_bid (q_action)
    WHERE q_action IS NOT NULL;

COMMIT;
