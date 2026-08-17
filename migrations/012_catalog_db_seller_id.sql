-- 012 (catalog_db): every product has an owner
--
-- The first step out of single-tenancy. Today one organisation owns every
-- product; in a marketplace a product belongs to a seller, and almost every
-- feature that follows -- seller dashboards, per-seller payouts, seller
-- ratings, order splitting, "shops near me" -- needs to know which one.
--
-- Deliberately the smallest possible step. There is no seller-service yet, so
-- seller_id is an opaque reference with an index and no foreign key: sellers
-- will be owned by another service and another database, and a foreign key
-- across a service boundary is a coupling this architecture forbids (Rule 1).
-- category_id has an FK because categories genuinely live in this database;
-- sellers will not.
--
-- Existing rows are backfilled to a single well-known seller. That id is a
-- sentinel, not a real merchant, and it is deliberately greppable: every place
-- that still assumes one seller shows up as a reference to it.
--
--   psql -U admin -d catalog_db -f migrations/012_catalog_db_seller_id.sql
--
-- Idempotent.

ALTER TABLE public.products
    ADD COLUMN IF NOT EXISTS seller_id uuid;

-- The platform seller. Every product that existed before sellers did belongs
-- to it, and it is the default until seller-service can issue real ids.
UPDATE public.products
SET seller_id = '00000000-0000-0000-0000-000000000001'
WHERE seller_id IS NULL;

ALTER TABLE public.products
    ALTER COLUMN seller_id SET NOT NULL;

-- Every seller-facing query filters on this: a seller's own catalogue, their
-- order lines, their metrics. Without the index each of those is a full scan
-- of every product on the platform.
CREATE INDEX IF NOT EXISTS ix_products_seller_id
    ON public.products (seller_id);

-- A seller's catalogue is almost always browsed newest-first.
CREATE INDEX IF NOT EXISTS ix_products_seller_created
    ON public.products (seller_id, created_at DESC);
