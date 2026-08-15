-- 003 (promo_db only): create the promotion-service schema
--
-- promo_db was completely empty: no promotions table, and neither of the
-- per-service scaffolding tables. Same root cause as migration 002 — models
-- existed in code, nothing ever applied them to Postgres.
--
-- Rule 1: this file targets promo_db ONLY. idempotency_keys and
-- outbox_messages are per-service tables, never shared; each service owns
-- its own copy inside its own database.
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--
--   psql -U admin -d promo_db -f migrations/003_promo_db_create_tables.sql
--
-- Idempotent: primary keys declared inline, indexes use IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS public.promotions (
    id                 uuid                     NOT NULL PRIMARY KEY,
    code               character varying(50)    NOT NULL,
    discount_percent   integer                  NOT NULL,
    max_discount_cents integer                  NOT NULL,
    is_active          boolean                  NOT NULL,
    created_at         timestamp with time zone,
    expires_at         timestamp with time zone
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_promotions_code
    ON public.promotions (code);

-- ---------------- per-service scaffolding ----------------

CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key        character varying(255) NOT NULL PRIMARY KEY,
    created_at timestamp with time zone
);

-- Present for parity with the saga-participating services (Rule 4). Harmless
-- where unused; required the moment this service joins a saga path.
ALTER TABLE public.idempotency_keys ADD COLUMN IF NOT EXISTS result_id uuid;

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
