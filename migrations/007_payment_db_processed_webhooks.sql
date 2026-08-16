-- 007 (payment_ledger_db): durable webhook deduplication
--
-- Layer 3 of the four in §4. Redis (layer 2) answers the common case cheaply,
-- but it is memory: a flush, a restart without persistence, or an eviction
-- turns every past webhook into a new one, and a PSP replaying a week of
-- events would then be charged straight through to the outbox.
--
-- Owned by payment-service's database rather than by webhook-handler, which
-- has no database of its own (§3b). The handler writes here and to
-- outbox_messages in the same transaction, so an event can never be marked
-- processed without the event it produced.
--
--   psql -U admin -d payment_ledger_db -f migrations/007_payment_db_processed_webhooks.sql

CREATE TABLE IF NOT EXISTS processed_webhooks (
    -- The PSP's own event id. PRIMARY KEY is the dedup: a second delivery of
    -- the same event cannot insert, and ON CONFLICT DO NOTHING turns that into
    -- a rowcount of zero rather than an error.
    event_id     TEXT        NOT NULL PRIMARY KEY,
    event_type   TEXT        NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Retention sweeps by age, and the dedup window is 7 days.
CREATE INDEX IF NOT EXISTS ix_processed_webhooks_received_at
    ON processed_webhooks (received_at);
