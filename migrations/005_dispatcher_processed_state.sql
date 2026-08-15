-- 005 (order_db): dispatcher bookkeeping
--
-- Two things the saga dispatcher needs, neither of which existed.
--
-- 1. outbox_messages.processed_at
--    The prototype worker marked a command done by REWRITING its type to
--    "<type>_Processed". That mutates a row Debezium is streaming, so CDC
--    emits a second phantom event for a command that already ran, and it
--    destroys the original type so the row can never be replayed or audited.
--    An outbox row is an immutable record of intent; completion is separate
--    state.
--
-- 2. processed_events
--    Rule 4 requires consumers to record what they have applied, in the same
--    transaction as their business logic, so redelivery is a no-op. It was
--    mandated but never defined and therefore never built.
--
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--   psql -U admin -d order_db -f migrations/005_dispatcher_processed_state.sql
--
-- Idempotent.

-- Created here as well as altered: on a fresh cluster there is no create_all
-- before the migration Job, so a bare ALTER fails on an empty database.
CREATE TABLE IF NOT EXISTS public.outbox_messages (
    id             uuid                   NOT NULL PRIMARY KEY,
    aggregate_type character varying(255) NOT NULL,
    aggregate_id   character varying(255) NOT NULL,
    type           character varying(255) NOT NULL,
    payload        jsonb                  NOT NULL,
    created_at     timestamp with time zone
);

ALTER TABLE public.outbox_messages
    ADD COLUMN IF NOT EXISTS processed_at timestamp with time zone;

-- The dispatcher claims work with
--   WHERE type LIKE '%Command' AND processed_at IS NULL ... FOR UPDATE SKIP LOCKED
-- so a partial index keeps the claim query cheap as the outbox grows.
CREATE INDEX IF NOT EXISTS ix_outbox_unprocessed_commands
    ON public.outbox_messages (created_at)
    WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS public.processed_events (
    event_id     uuid                     NOT NULL,
    consumer     text                     NOT NULL,
    event_type   text                     NOT NULL,
    processed_at timestamp with time zone NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, consumer)
);

-- Composite key, not event_id alone: two different consumers may legitimately
-- process the same event, and each needs its own record of having done so.
CREATE INDEX IF NOT EXISTS ix_processed_events_processed_at
    ON public.processed_events (processed_at);
