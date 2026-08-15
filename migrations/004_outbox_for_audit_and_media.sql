-- 004 (audit_db and media_meta_db): create the outbox table
--
-- Both databases were missing outbox_messages entirely, so their Debezium
-- connectors had no table to capture (table.include.list points at
-- public.outbox_messages) and those services could never publish an event.
--
-- Rule 3: outbox before Kafka. A service with no outbox table cannot
-- participate in the event mesh at all.
-- Rule 1: outbox_messages is per-service and never shared.
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--
--   psql -U admin -d audit_db      -f migrations/004_outbox_for_audit_and_media.sql
--   psql -U admin -d media_meta_db -f migrations/004_outbox_for_audit_and_media.sql
--
-- Idempotent: primary key inline, index uses IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS public.outbox_messages (
    id             uuid                   NOT NULL PRIMARY KEY,
    aggregate_type character varying(255) NOT NULL,
    aggregate_id   character varying(255) NOT NULL,
    type           character varying(255) NOT NULL,
    payload        jsonb                  NOT NULL,
    created_at     timestamp with time zone
);

-- The retention job prunes by age; Debezium reads in insertion order.
CREATE INDEX IF NOT EXISTS ix_outbox_messages_created_at
    ON public.outbox_messages (created_at);

CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key        character varying(255) NOT NULL PRIMARY KEY,
    created_at timestamp with time zone
);

ALTER TABLE public.idempotency_keys ADD COLUMN IF NOT EXISTS result_id uuid;
