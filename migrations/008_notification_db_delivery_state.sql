-- 008 (notification_db): delivery state for notification-worker
--
-- notification-service owns the notification; the worker owns its delivery
-- (§3b). Delivery needs state the service never had: how many times it has
-- been attempted, when it may next be tried, and which worker is holding it.
--
-- Same claim-lease shape as 006 for the saga dispatcher, and for the same
-- reason: the worker must never hold a database transaction across a call to
-- an external provider. It claims in one short transaction, dispatches with no
-- transaction open, then settles in another. A claim is a lease, so a worker
-- that dies mid-send does not strand rows -- any claim older than the lease
-- window is reclaimable, which makes delivery at-least-once rather than
-- at-most-once. For notifications that is the right trade: a duplicate email
-- is embarrassing, a silently dropped password reset is a support ticket.
--
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--   psql -U admin -d notification_db -f migrations/008_notification_db_delivery_state.sql
--
-- Idempotent.

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamp with time zone DEFAULT NOW();

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS claimed_at timestamp with time zone;

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS claimed_by text;

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS last_error text;

ALTER TABLE public.notification_records
    ADD COLUMN IF NOT EXISTS delivered_at timestamp with time zone;

-- The claim query filters on exactly these three columns. Without the index it
-- is a sequential scan of every notification ever sent, repeated every poll.
CREATE INDEX IF NOT EXISTS ix_notification_records_claimable
    ON public.notification_records (status, next_attempt_at, claimed_at);
