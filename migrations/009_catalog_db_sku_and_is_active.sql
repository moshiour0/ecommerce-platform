-- 009 (catalog_db): give products the sku and is_active they always claimed
--
-- Both fields existed everywhere except in the table. The API accepted a sku
-- and discarded it, the Elasticsearch mapping declared one, and the indexer
-- read payload.get("sku") -- which was always None, because catalog never put
-- it in the event. Every indexed product had a null SKU. is_active was worse:
-- accepted, never stored, echoed back as true by a response-schema default, and
-- indexed as true, so is_active=false was impossible to express.
--
-- Existing rows need a sku before the column can be NOT NULL and UNIQUE, and
-- there is no real value to give them -- the ones their creators sent were
-- thrown away. A deterministic placeholder derived from the primary key is used
-- instead: unique by construction, obviously synthetic to anyone reading it,
-- and stable if this migration is applied twice.
--
--   psql -U admin -d catalog_db -f migrations/009_catalog_db_sku_and_is_active.sql
--
-- Idempotent.

-- Nullable first: the backfill cannot run against a NOT NULL column.
ALTER TABLE public.products
    ADD COLUMN IF NOT EXISTS sku varchar(64);

ALTER TABLE public.products
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;

-- 'SKU-' plus the first ten hex characters of the id. Upper case to match the
-- normalisation the service applies, and derived from a UUID so collisions are
-- not a practical concern.
UPDATE public.products
SET sku = 'SKU-' || upper(substring(replace(id::text, '-', '') from 1 for 10))
WHERE sku IS NULL;

ALTER TABLE public.products
    ALTER COLUMN sku SET NOT NULL;

-- Unique because a SKU identifies the product to everything outside this
-- system. Created as an index rather than a table constraint so IF NOT EXISTS
-- applies and re-running is safe.
CREATE UNIQUE INDEX IF NOT EXISTS ux_products_sku
    ON public.products (sku);
