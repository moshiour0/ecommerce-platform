-- 016 (order_db): order lines, and one seller order per seller
--
-- Until now an order stored a user, a status and a total. What was actually
-- ordered lived only inside an outbox event payload on its way to the
-- inventory reservation, and was never written to order_db at all. That was
-- survivable for a single-tenant shop and is not survivable for a
-- marketplace: every step after checkout -- courier collection, payout,
-- commission, returns, seller metrics -- is per seller, and none of them can
-- be answered from a total.
--
-- So two tables. `order_lines` is what the buyer bought. `seller_orders` is
-- the same thing grouped by who has to ship it, which is the unit everything
-- downstream actually operates on (ARCHITECTURE_STATE_FINAL.md §3d).
--
-- The buyer-facing status on order_saga_states stays where it is and is NOT
-- extended with the COD vocabulary. It is derived from the seller orders by
-- split_rules.derive_order_status, and deriving it is the point: two stored
-- answers to "is this order delivered" disagree the first time one seller is
-- late, and the one displayed is whichever the query happened to read.
--
-- No foreign key to seller_db or catalog_db. Rule 1 -- seller_id and
-- product_id are opaque references to other services' databases, exactly as
-- products.seller_id is (migration 012).
--
--   psql -U admin -d order_db -f migrations/016_order_db_seller_orders.sql
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.seller_orders (
    id             uuid PRIMARY KEY,
    order_id       uuid        NOT NULL,
    seller_id      uuid        NOT NULL,

    -- The COD lifecycle in §3d. Born PENDING like its parent; nothing here
    -- advances it yet, which is deliberate -- moving a seller order through
    -- dispatch, delivery and settlement is the next piece of work, and a
    -- column that can only be written by code that does not exist is better
    -- than a column invented later under a live order.
    status         varchar(32) NOT NULL DEFAULT 'PENDING',
    status_reason  varchar(1024),

    -- Goods only, for this seller. NOT the parent's total_cents, which also
    -- carries tax, shipping and promotions. Allocating those across sellers
    -- decides what each seller is paid and what commission is charged on, so
    -- it is a finance decision and is deliberately not made here.
    subtotal_cents integer     NOT NULL,
    currency       varchar(3)  NOT NULL DEFAULT 'BDT',
    item_count     integer     NOT NULL,

    created_at     timestamp with time zone DEFAULT NOW(),
    updated_at     timestamp with time zone DEFAULT NOW()
);

-- One seller order per seller per order. A retried checkout that got halfway
-- must not produce a second seller order for the same seller: the split is
-- deterministic, so the second attempt would create a duplicate that both the
-- payout and the courier would act on.
CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_orders_order_seller
    ON public.seller_orders (order_id, seller_id);

-- "Everything in this order", the parent status derivation's read.
CREATE INDEX IF NOT EXISTS ix_seller_orders_order
    ON public.seller_orders (order_id);

-- "This seller's orders", which is the seller dashboard and every payout run.
CREATE INDEX IF NOT EXISTS ix_seller_orders_seller_created
    ON public.seller_orders (seller_id, created_at DESC);

-- The queue a seller works from, and the one an operator watches for orders
-- stuck before dispatch.
CREATE INDEX IF NOT EXISTS ix_seller_orders_seller_status
    ON public.seller_orders (seller_id, status);

CREATE TABLE IF NOT EXISTS public.order_lines (
    id               uuid PRIMARY KEY,
    order_id         uuid        NOT NULL,

    -- Which seller order this line belongs to. Nullable only so the column can
    -- be added to a table that already has rows; every line written from here
    -- on carries it.
    seller_order_id  uuid,

    product_id       uuid        NOT NULL,
    seller_id        uuid        NOT NULL,
    quantity         integer     NOT NULL CHECK (quantity > 0),

    -- Unit price at the time of checkout, not a live lookup. A price that
    -- moves after an order is placed must not change what the buyer owes or
    -- what the seller is paid.
    price_cents      integer     NOT NULL CHECK (price_cents >= 0),
    line_total_cents integer     NOT NULL CHECK (line_total_cents >= 0),
    currency         varchar(3)  NOT NULL DEFAULT 'BDT',

    created_at       timestamp with time zone DEFAULT NOW()
);

-- One line per product per order. plan_item_reservations already coalesces
-- duplicate lines before reserving, so two rows for one product in one order
-- would describe stock that was never held separately.
CREATE UNIQUE INDEX IF NOT EXISTS uq_order_lines_order_product
    ON public.order_lines (order_id, product_id);

CREATE INDEX IF NOT EXISTS ix_order_lines_order
    ON public.order_lines (order_id);

CREATE INDEX IF NOT EXISTS ix_order_lines_seller_order
    ON public.order_lines (seller_order_id);
