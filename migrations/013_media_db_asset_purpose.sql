-- 013 (media_meta_db): what each asset is for
--
-- The Media Center seam (MARKETPLACE_ROADMAP.md §3.6). Media Center is not
-- built and will not be for some time, but this column is cheap now and
-- expensive later: purpose decides who may read an asset, and retrofitting
-- that onto a store full of untyped files means auditing every object in it.
--
-- Four purposes, and the split that matters is not media type but confidence:
--
--   product_image     a listing photo: public once scanned clean
--   post_media        shared to the feed deliberately: public once clean
--   seller_document   KYC, trade licence, bank details: never public
--   try_on_source     a photograph of a person's body: never public
--
-- try_on_source is the reason to do this now rather than when Media Center
-- starts. It is the most sensitive content this platform will ever hold, it is
-- indistinguishable from a product photo by file type, and the code that
-- decides "is this servable" already exists. Adding the distinction after
-- try-on ships means the first version of it is written by someone who has to
-- remember.
--
-- Existing rows are product images: that is all this service has ever stored.
--
--   psql -U admin -d media_meta_db -f migrations/013_media_db_asset_purpose.sql
--
-- Idempotent.

ALTER TABLE public.media_assets
    ADD COLUMN IF NOT EXISTS purpose varchar(32);

UPDATE public.media_assets
SET purpose = 'product_image'
WHERE purpose IS NULL;

ALTER TABLE public.media_assets
    ALTER COLUMN purpose SET DEFAULT 'product_image';

ALTER TABLE public.media_assets
    ALTER COLUMN purpose SET NOT NULL;

-- Rejects a purpose the application does not know about. A new kind of upload
-- has to be added deliberately, in the rules and here, rather than arriving as
-- a string nobody has classified as confidential or public.
ALTER TABLE public.media_assets
    DROP CONSTRAINT IF EXISTS ck_media_assets_purpose;
ALTER TABLE public.media_assets
    ADD CONSTRAINT ck_media_assets_purpose
    CHECK (purpose IN ('product_image', 'seller_document',
                       'try_on_source', 'post_media'));

-- "Every document belonging to this seller", "every try-on source for this
-- user" -- both are owner plus purpose, and both are how a data-deletion
-- request gets answered.
CREATE INDEX IF NOT EXISTS ix_media_assets_owner_purpose
    ON public.media_assets (owner_id, purpose);
