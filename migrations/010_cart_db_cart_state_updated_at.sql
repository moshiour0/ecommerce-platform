-- 010 (cart_db): the column the checkout reclaim sweep has always filtered on
--
-- The sweeper's second pass reclaims carts stuck in checkout_in_progress for
-- more than ten minutes (§3, cart-service). It filtered on cart_state.updated_at,
-- and that column has never existed, so the query failed with "column
-- updated_at does not exist" on every run since the sweeper was written. The
-- result was 34 carts stuck in checkout_in_progress permanently -- each one a
-- cart that can never be checked out again, holding inventory that is never
-- released.
--
-- Existing rows are backfilled from created_at rather than NOW(). NOW() would
-- restart everyone's ten-minute clock and hide the backlog for another ten
-- minutes; created_at is the truthful answer to "when do we last know this row
-- changed", and it means the first sweep after this migration reclaims the
-- carts that have been stuck for days. That mass reclaim is the point, not a
-- side effect.
--
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--   psql -U admin -d cart_db -f migrations/010_cart_db_cart_state_updated_at.sql
--
-- Idempotent.

ALTER TABLE public.cart_state
    ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone;

UPDATE public.cart_state
SET updated_at = COALESCE(created_at, NOW())
WHERE updated_at IS NULL;

ALTER TABLE public.cart_state
    ALTER COLUMN updated_at SET DEFAULT NOW();

ALTER TABLE public.cart_state
    ALTER COLUMN updated_at SET NOT NULL;

-- Both sweeps filter on status and a timestamp. Without an index each pass is a
-- sequential scan of every cart ever created, once a minute, forever.
CREATE INDEX IF NOT EXISTS ix_cart_state_status_updated_at
    ON public.cart_state (status, updated_at);

CREATE INDEX IF NOT EXISTS ix_cart_state_status_expires_at
    ON public.cart_state (status, expires_at);
