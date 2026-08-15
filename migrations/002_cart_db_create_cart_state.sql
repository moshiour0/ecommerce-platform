-- 002 (cart_db only): create cart_state
--
-- The C-1/C-4 checkout-mutex work added a CartState model, but no service
-- calls Base.metadata.create_all() and no Alembic migration was ever authored
-- (16 alembic/ scaffolds, zero versions/). The table never reached Postgres,
-- so every add-to-cart raised UndefinedTableError and returned HTTP 500.
--
-- Rule 1: this file targets cart_db ONLY. Do not apply it to another database.
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--
--   psql -U admin -d cart_db -f migrations/002_cart_db_create_cart_state.sql
--
-- Idempotent: primary key declared inline, indexes use IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS public.cart_state (
    cart_id    uuid                     NOT NULL PRIMARY KEY,
    user_id    uuid                     NOT NULL,
    items      jsonb                    NOT NULL,
    status     character varying(50)    NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone
);

CREATE INDEX IF NOT EXISTS ix_cart_state_user_id
    ON public.cart_state (user_id);

-- The TTL sweeper filters on status and expires_at together, and the atomic
-- active -> checkout_in_progress transition filters on (user_id, status).
CREATE INDEX IF NOT EXISTS ix_cart_state_status_expires
    ON public.cart_state (status, expires_at);
