-- 018 (fulfillment_db): shipments and courier settlement
--
-- fulfillment-service stored a FulfillmentRecord with a hardcoded courier of
-- "SwissPost", a tracking number invented from a uuid, and a mock hazard check
-- on a region called "Blatten". That was demo scaffolding for a single-tenant
-- shop in Europe. This market has many couriers, none of them SwissPost, and
-- the cash arrives through them.
--
-- Two tables.
--
-- `shipments` is one parcel: which seller order it carries, which courier has
-- it, and the last thing that courier said. The courier's own word is kept
-- alongside the canonical status it mapped to -- when a courier adds a status
-- nobody has mapped, the raw word is the only evidence of what happened, and
-- discarding it would leave a parcel stuck with no way to find out why.
--
-- `settlement_rows` is the cash. Under COD the courier collects at the door
-- and remits later in a batch, and that file is the only evidence the platform
-- has that it was paid. Every row is stored, including the ones that did not
-- reconcile: a row rejected and forgotten is a dispute nobody can reconstruct.
--
-- No foreign keys to order_db. Rule 1 -- seller_order_id is an opaque
-- reference to another service's database.
--
--   psql -U admin -d fulfillment_db -f migrations/018_fulfillment_db_shipments.sql
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.shipments (
    id                uuid PRIMARY KEY,
    seller_order_id   uuid        NOT NULL,
    order_id          uuid        NOT NULL,

    provider          varchar(64) NOT NULL,
    tracking_code     varchar(128),

    -- What the platform believes, from courier_rules.CourierStatus.
    status            varchar(32) NOT NULL DEFAULT 'PICKUP_PENDING',

    -- What the courier actually said, verbatim. Kept because an unmapped
    -- status is exactly the case where the canonical column is empty and this
    -- one is the only record of the event.
    raw_status        varchar(128),
    raw_status_at     timestamp with time zone,

    -- Set when a courier sends a word this provider's mapping does not cover.
    -- Parked for a human rather than dropped: couriers add statuses without
    -- announcing them, and the alternative is a parcel that silently stops
    -- moving.
    unmapped          boolean     NOT NULL DEFAULT false,

    -- What the platform expects the courier to collect at the door, copied at
    -- creation. Copied rather than looked up at settlement time because the
    -- amount owed is what was agreed when the parcel went out, not what the
    -- order says weeks later.
    cod_amount_cents  integer     NOT NULL DEFAULT 0,
    currency          varchar(3)  NOT NULL DEFAULT 'BDT',

    created_at        timestamp with time zone DEFAULT NOW(),
    updated_at        timestamp with time zone DEFAULT NOW()
);

-- One shipment per seller order. A second parcel for the same seller order
-- would mean two couriers both collecting cash for it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shipments_seller_order
    ON public.shipments (seller_order_id);

-- The courier's own reference is how a callback and a settlement row find the
-- shipment, so it has to be unique within that courier.
CREATE UNIQUE INDEX IF NOT EXISTS uq_shipments_provider_tracking
    ON public.shipments (provider, tracking_code)
    WHERE tracking_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_shipments_order
    ON public.shipments (order_id);

-- The operator's queue: parcels whose courier said something nobody mapped.
CREATE INDEX IF NOT EXISTS ix_shipments_unmapped
    ON public.shipments (raw_status_at)
    WHERE unmapped;

CREATE TABLE IF NOT EXISTS public.settlement_rows (
    id               uuid PRIMARY KEY,
    provider         varchar(64)  NOT NULL,

    -- The courier's batch reference, so a resent file is recognisable as the
    -- same batch rather than as new money.
    batch_reference  varchar(128) NOT NULL,
    row_reference    varchar(128) NOT NULL,

    seller_order_id  uuid,
    expected_cents   integer      NOT NULL DEFAULT 0,
    collected_cents  integer      NOT NULL DEFAULT 0,
    currency         varchar(3)   NOT NULL DEFAULT 'BDT',

    -- From courier_rules.ReconcileOutcome. Rows that did not reconcile are
    -- stored too: a rejected row that is forgotten is a dispute nobody can
    -- reconstruct.
    outcome          varchar(32)  NOT NULL,
    detail           varchar(1024),

    created_at       timestamp with time zone DEFAULT NOW()
);

-- One row per reference per batch. This is what makes a resent file a no-op
-- rather than a second payout.
CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_batch_row
    ON public.settlement_rows (provider, batch_reference, row_reference);

-- "Has this seller order been settled already", asked once per row.
CREATE INDEX IF NOT EXISTS ix_settlement_seller_order
    ON public.settlement_rows (seller_order_id);

-- The reconciliation queue.
CREATE INDEX IF NOT EXISTS ix_settlement_attention
    ON public.settlement_rows (created_at)
    WHERE outcome NOT IN ('matched', 'duplicate');
