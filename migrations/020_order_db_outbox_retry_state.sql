-- Retry state for the outbox, so a command that cannot succeed stops
-- blocking the ones that can.
--
-- Before this, a failed command had exactly two fates: settled (processed_at
-- set, never seen again) or released (claimed_at nulled, instantly
-- re-claimable). The second is what the escrow and stock-movement handlers
-- chose, under the reasoning "never abandon an unbooked liability".
--
-- Never abandoning meant never progressing. CLAIM_SQL orders by created_at, so
-- the oldest unprocessed rows are claimed first on every tick. Fourteen escrow
-- bookings for sellers that no longer existed -- each a permanent 409 -- were
-- re-claimed ahead of all live work every two seconds, filled the batch of
-- twenty, and saturated the payment-service bulkhead until legitimate bookings
-- were shed. A live seller's real liability went unbooked for fourteen minutes
-- because fourteen dead ones were ahead of it.
--
-- Three columns fix it:
--
--   attempts        how many times this row has been tried. Caps the retrying.
--   next_attempt_at not claimable before this. Turns "retry" into "retry
--                   later", which is what stops the head-of-line block: a
--                   backing-off row leaves the batch instead of filling it.
--   parked_at       stopped. Kept, not deleted, and deliberately distinct from
--                   processed_at -- a processed row is indistinguishable from
--                   one that succeeded, and an unbooked liability must never
--                   look like a booked one.
--
-- All three are nullable with no default backfill, so existing rows keep their
-- current behaviour: NULL next_attempt_at means "claimable now", which is
-- exactly what every row means today.

ALTER TABLE outbox_messages
    ADD COLUMN IF NOT EXISTS attempts        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS parked_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error      TEXT;

-- The claimable index has to know about parking and backoff or the planner
-- still walks every dead row on each poll. Partial on the same predicate the
-- claim uses, so a parked backlog costs nothing to skip.
DROP INDEX IF EXISTS ix_outbox_claimable;
CREATE INDEX IF NOT EXISTS ix_outbox_claimable
    ON outbox_messages (next_attempt_at NULLS FIRST, created_at)
    WHERE processed_at IS NULL AND parked_at IS NULL;

-- Parked rows are a queue for people, so they need to be findable as one.
CREATE INDEX IF NOT EXISTS ix_outbox_parked
    ON outbox_messages (parked_at DESC)
    WHERE parked_at IS NOT NULL;
