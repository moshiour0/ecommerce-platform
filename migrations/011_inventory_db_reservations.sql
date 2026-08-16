-- 011 (inventory_db): a ledger of what each order actually holds
--
-- §5 declares ReleaseInventoryCommand as the inverse of ReserveInventoryCommand,
-- and the platform could not perform it, because nothing recorded which units
-- belonged to which order. inventory_items carries two counters and no history;
-- order_saga_states has no items column. So neither side could answer "what
-- should this order give back", and the dispatcher settled the compensation by
-- acknowledging it to itself without calling anyone. 75 rows holding 88 units
-- were stranded that way, and every failed order leaked more.
--
-- The ledger lives here rather than in the saga because inventory owns stock
-- (Rule 1). It also makes the blind-compensation contract in §5 fall out for
-- free: releasing an order that never reserved matches no rows, which is a
-- no-op and a success rather than an error.
--
-- Rule 10: run as a Job or InitContainer, never in the app startup path.
--   psql -U admin -d inventory_db -f migrations/011_inventory_db_reservations.sql
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.inventory_reservations (
    id          uuid        NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Text, not uuid: the saga's order id arrives as a string and a
    -- compensation must never fail on a cast.
    order_id    text        NOT NULL,
    product_id  uuid        NOT NULL,
    quantity    integer     NOT NULL CHECK (quantity > 0),
    -- held -> released. Rows are never deleted: an audit of what was returned
    -- is worth more than the space, and a deleted row is indistinguishable
    -- from one that never existed.
    status      text        NOT NULL DEFAULT 'held'
                            CHECK (status IN ('held', 'released')),
    created_at  timestamptz NOT NULL DEFAULT NOW(),
    released_at timestamptz
);

-- One live hold per order and product. Delivery is at-least-once, so the same
-- ReserveInventoryCommand can arrive twice; without this the second delivery
-- would record a second hold and the release would give back twice what was
-- taken. Partial, so the released history does not block a later re-reservation.
CREATE UNIQUE INDEX IF NOT EXISTS ux_inventory_reservations_live
    ON public.inventory_reservations (order_id, product_id)
    WHERE status = 'held';

-- The release path looks up by order alone.
CREATE INDEX IF NOT EXISTS ix_inventory_reservations_order
    ON public.inventory_reservations (order_id, status);
