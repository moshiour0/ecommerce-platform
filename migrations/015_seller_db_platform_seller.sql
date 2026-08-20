-- 015 (seller_db): the platform seller becomes a real row
--
-- migrations/012 backfilled every pre-marketplace product to the sentinel
-- seller 00000000-0000-0000-0000-000000000001 and said plainly that it was a
-- sentinel and not a merchant.
--
-- catalog-service now refuses a listing from a seller who may not sell, and it
-- asks seller-service. That leaves the sentinel with two possible treatments:
-- exempt it from the check, or make it a seller. Exempting it would put a
-- permanent hole in the control -- a special-case id that skips verification
-- is exactly the shape of thing that later gets reused for "internal" listings
-- and then for whatever else is inconvenient to onboard.
--
-- So it is seeded as a real, active seller instead. It goes through the same
-- gate as anybody else and passes it, which is both honest and what a
-- marketplace's own first-party shop actually is -- Daraz Mall, in the
-- reference model. It stays greppable: the id is unchanged, and every
-- reference to it is still a place that assumes a single tenant.
--
-- Registered and approved in the same statement rather than driven through the
-- state machine, because there is no seller to submit documents and no
-- reviewer to look at them. That is a seed, not a shortcut a route can take:
-- seller-service exposes no way to reach 'active' without a review, which is
-- what tests/unit/test_seller_rules.py proves by searching the whole graph.
--
--   psql -U admin -d seller_db -f migrations/015_seller_db_platform_seller.sql
--
-- Idempotent.

INSERT INTO public.sellers (
    id,
    legal_name,
    display_name,
    contact_email,
    status,
    status_reason,
    accepted_contract_version,
    contract_accepted_at,
    country,
    created_at,
    updated_at
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    'Platform First-Party Seller',
    'Marketplace',
    'platform@example.invalid',
    'active',
    'seeded by migration 015; the platform''s own first-party shop',
    -- Must match seller_rules.CURRENT_CONTRACT_VERSION. If that is ever
    -- raised, this row needs a follow-up migration accepting the new version,
    -- or the platform's own listings start being refused for stale terms --
    -- which is the contract check working correctly on a seller nobody
    -- remembered to re-accept for.
    1,
    NOW(),
    'BD',
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;
