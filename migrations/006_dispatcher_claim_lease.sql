-- 006 (order_db): claim lease for the saga dispatcher
--
-- The first dispatcher held a Postgres transaction open for the whole batch:
-- it selected rows FOR UPDATE SKIP LOCKED and then made every downstream HTTP
-- call inside that transaction. With a batch of 20 and a 10s timeout, one
-- transaction could stay open ~200 seconds while holding row locks and a
-- pooled connection, blocking VACUUM and pinning WAL that Debezium must scan.
-- Never hold a database transaction across network I/O.
--
-- The fix splits the work into two short transactions with the HTTP calls
-- outside both:
--   1. claim  -- one statement, stamps claimed_at/claimed_by, commits
--   2. work   -- no transaction held
--   3. settle -- one statement, sets processed_at or clears the claim
--
-- A claim is a lease, not a lock, so a dispatcher that crashes mid-flight
-- does not strand its rows: any claim older than the lease window is
-- reclaimable. Delivery is therefore at-least-once, which is safe because
-- every downstream call carries an Idempotency-Key and processed_events
-- dedupes on replay.
--
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--   psql -U admin -d order_db -f migrations/006_dispatcher_claim_lease.sql
--
-- Idempotent.

ALTER TABLE public.outbox_messages
    ADD COLUMN IF NOT EXISTS claimed_at timestamp with time zone;

ALTER TABLE public.outbox_messages
    ADD COLUMN IF NOT EXISTS claimed_by text;

-- The claim query filters on processed_at IS NULL and orders by created_at.
-- Replaces the previous partial index, which did not cover the claim column.
DROP INDEX IF EXISTS ix_outbox_unprocessed_commands;

CREATE INDEX IF NOT EXISTS ix_outbox_claimable
    ON public.outbox_messages (created_at)
    WHERE processed_at IS NULL;
