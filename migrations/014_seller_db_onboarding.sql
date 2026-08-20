-- 014 (seller_db): sellers, their KYC documents, and the outbox
--
-- migrations/012 gave every product a seller_id and said plainly that there
-- was no seller-service yet and that the id was an opaque reference to a
-- service that did not exist. This is that service's schema.
--
-- Rule 1: seller_db is owned by seller-service alone. catalog_db keeps
-- products.seller_id with an index and no foreign key, because a foreign key
-- across a service boundary is the coupling this architecture forbids. That
-- stays true in both directions -- nothing here references catalog_db either.
--
-- Everything is created by the service's SQLAlchemy models as well; this file
-- exists for the constraints and indexes that models do not express well, and
-- so a database can be brought to the right shape without booting the service.
--
--   psql -U admin -d seller_db -f migrations/014_seller_db_onboarding.sql
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.sellers (
    id                        uuid PRIMARY KEY,

    legal_name                varchar(255) NOT NULL,
    display_name              varchar(255) NOT NULL,
    contact_email             varchar(320) NOT NULL,
    contact_phone             varchar(32),

    -- Born unable to sell. The default is here as well as in the models and
    -- in seller_rules, so a row inserted by any path at all -- a fixture, an
    -- admin tool, a later migration -- still starts without permission.
    status                    varchar(32)  NOT NULL DEFAULT 'registered',
    status_reason             varchar(1024),

    accepted_contract_version integer,
    contract_accepted_at      timestamp with time zone,

    address_line              varchar(512),
    city                      varchar(128),
    district                  varchar(128),
    country                   varchar(2)   NOT NULL DEFAULT 'BD',
    latitude                  varchar(32),
    longitude                 varchar(32),

    created_at                timestamp with time zone DEFAULT NOW(),
    updated_at                timestamp with time zone DEFAULT NOW()
);

-- The review queue is `WHERE status = 'documents_submitted'`, and the seller
-- directory is `WHERE status = 'active'`. Both are the hot reads.
CREATE INDEX IF NOT EXISTS ix_sellers_status
    ON public.sellers (status);

-- "Shops in this area" (roadmap §3.4) filters on district and city before it
-- ever does distance arithmetic. Partial on active sellers because a
-- suspended shop should not appear in a location search at all, and that is
-- most of the table's selectivity.
CREATE INDEX IF NOT EXISTS ix_sellers_location_active
    ON public.sellers (country, district, city)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.seller_documents (
    id            uuid PRIMARY KEY,
    seller_id     uuid        NOT NULL,
    document_type varchar(32) NOT NULL,

    -- media-service's id. No foreign key: another service, another database.
    -- Nothing from *inside* the document is stored here -- no national ID
    -- number, no licence number, no account number. The bytes stay in object
    -- storage under a confidential prefix; copying the numbers out would put
    -- the most sensitive data the platform holds in a second, less guarded
    -- place.
    media_id      uuid        NOT NULL,
    submitted_at  timestamp with time zone DEFAULT NOW()
);

-- One current document per type per seller. A resubmission replaces rather
-- than accumulates, so "which trade licence did the reviewer look at" has
-- exactly one answer.
CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_document_type
    ON public.seller_documents (seller_id, document_type);

CREATE INDEX IF NOT EXISTS ix_seller_documents_seller
    ON public.seller_documents (seller_id);

-- Rule 3: state changes and their events commit together.
CREATE TABLE IF NOT EXISTS public.outbox_messages (
    id             uuid PRIMARY KEY,
    aggregate_type varchar(255) NOT NULL,
    aggregate_id   varchar(255) NOT NULL,
    type           varchar(255) NOT NULL,
    payload        jsonb        NOT NULL,
    created_at     timestamp with time zone DEFAULT NOW()
);

-- Rule 4.
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key        varchar(255) PRIMARY KEY,
    result_id  uuid,
    created_at timestamp with time zone DEFAULT NOW()
);
