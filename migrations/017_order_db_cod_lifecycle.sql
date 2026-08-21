-- 017 (order_db): the cash-on-delivery lifecycle
--
-- migration 016 created seller_orders and left every one of them at PENDING,
-- because nothing could move them. This adds what moving them requires: the
-- payment method that decides which lifecycle an order follows, and the
-- timestamps and courier details a seller order accumulates as it travels.
--
-- Why payment_method exists on the order and not just in a comment
-- ----------------------------------------------------------------
-- The saga reaper sweeps orders stuck at PENDING, INVENTORY_RESERVED or PAID
-- for more than fifteen minutes and compensates them. Fifteen minutes is
-- correct for a card order, where the whole flow completes in seconds.
--
-- It is catastrophic for a COD order. Stock is held from checkout until
-- delivery, which is days, so the reaper would release the stock out from
-- under an order already on a van -- and then the goods arrive, the buyer
-- pays, and the platform has no record that it owed anybody anything.
--
-- So the reaper needs to know which kind of order it is looking at. COD orders
-- are still swept at PENDING, where a reservation that never came back really
-- is broken; they are not swept once stock is held.
--
-- Defaults to 'COD' because that is the primary path for this market
-- (ARCHITECTURE_STATE_FINAL.md section 3d), not because it is the safer
-- default. Existing rows are backfilled to 'CARD': they were placed under the
-- card flow and relabelling history would be a lie.
--
--   psql -U admin -d order_db -f migrations/017_order_db_cod_lifecycle.sql
--
-- Idempotent.

ALTER TABLE public.order_saga_states
    ADD COLUMN IF NOT EXISTS payment_method varchar(16) NOT NULL DEFAULT 'COD';

-- Everything that already existed was a card order. Only rows predating this
-- column: the DEFAULT above covers everything created afterwards.
UPDATE public.order_saga_states
SET payment_method = 'CARD'
WHERE payment_method = 'COD'
  AND created_at < NOW() - INTERVAL '1 second';

-- The reaper's candidate scan filters on this alongside status.
CREATE INDEX IF NOT EXISTS ix_order_saga_states_method_status
    ON public.order_saga_states (payment_method, status, updated_at);

-- When each step happened. Not derivable from updated_at, which only records
-- the most recent one, and every one of these is needed by something real:
-- dispatch-to-delivery time is a seller performance metric, delivery-to-
-- settlement is how long the platform is holding somebody else's cash, and
-- confirmed-but-not-dispatched is the queue an operator watches.
ALTER TABLE public.seller_orders
    ADD COLUMN IF NOT EXISTS confirmed_at  timestamp with time zone,
    ADD COLUMN IF NOT EXISTS dispatched_at timestamp with time zone,
    ADD COLUMN IF NOT EXISTS delivered_at  timestamp with time zone,
    ADD COLUMN IF NOT EXISTS settled_at    timestamp with time zone;

-- Which courier has the parcel, and their reference for it. Free text rather
-- than a foreign key to a couriers table: there is no courier integration yet
-- (section 7 step 11), and inventing its schema here would fix the shape
-- before any real provider has been read.
ALTER TABLE public.seller_orders
    ADD COLUMN IF NOT EXISTS courier_name  varchar(64),
    ADD COLUMN IF NOT EXISTS tracking_code varchar(128);

-- The operator's queue: confirmed a while ago and still not dispatched. A
-- partial index because it is a small slice of a table that will not be.
CREATE INDEX IF NOT EXISTS ix_seller_orders_awaiting_dispatch
    ON public.seller_orders (confirmed_at)
    WHERE status = 'CONFIRMED';

-- Delivered and not yet settled: cash the platform is holding and owes on.
CREATE INDEX IF NOT EXISTS ix_seller_orders_awaiting_settlement
    ON public.seller_orders (delivered_at)
    WHERE status = 'DELIVERED';
