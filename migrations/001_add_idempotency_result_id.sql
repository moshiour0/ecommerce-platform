-- 001: add idempotency_keys.result_id
--
-- The S-1 remediation added result_id to every IdempotencyKey model but no
-- migration was authored, and SQLAlchemy's create_all() never alters an
-- existing table. Every code path that reads result_id therefore raised
-- UndefinedColumnError, so all state-mutating endpoints in these five
-- services returned HTTP 500.
--
-- Rule 10: migrations run as a Job/InitContainer, never in the app startup
-- path. Apply with:
--   for db in order_db inventory_db payment_ledger_db fraud_db cart_db; do
--     psql -U admin -d $db -f migrations/001_add_idempotency_result_id.sql
--   done
--
-- Idempotent: safe to re-run.
--
-- The CREATE is not redundant. Under compose this file always ran after
-- bootstrap_schema.py's create_all phase, so the table already existed. On a
-- fresh Kubernetes cluster there is no create_all before the migration Job,
-- and the bare ALTER failed with 'relation "idempotency_keys" does not exist',
-- taking the whole Job down on its first statement. A migration must be able
-- to run against an empty database.
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key        character varying(255) NOT NULL PRIMARY KEY,
    created_at timestamp with time zone
);

ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS result_id UUID;
