-- Reviews: the input the ranking formula has been missing since it was written.
--
-- ARCHITECTURE §3g scores products as
--     text_relevance x quality_boost x distance_decay x personalisation
-- and quality_boost reads a rating and a review count that no service
-- produced. They were reported as None and treated as neutral -- honest, since
-- inventing an average would have put a number into the formula that looked
-- like evidence, but it left a third of the scoring inert.
--
-- Two decisions shape this table.
--
-- **Verified purchase only.** A review requires a delivered seller order
-- containing that product. Not because unverified reviews are always false,
-- but because under COD delivery is the one fact the platform already knows
-- for certain, and it is the strongest anti-gaming signal available without a
-- fraud model. An open review endpoint is a free vote, and free votes get
-- bought.
--
-- **Product and seller are rated separately.** They are different claims that
-- one star cannot carry: an excellent product packed badly, or a mediocre
-- product from a seller who did everything right. Ranking needs the seller
-- signal; buyers think in products.

CREATE TABLE IF NOT EXISTS reviews (
    id               UUID PRIMARY KEY,

    -- References into other services' data. No foreign keys: Rule 1 means
    -- review_db cannot see order_db or catalog_db, which is exactly why
    -- verifying a purchase is an HTTP call rather than a join.
    seller_order_id  UUID        NOT NULL,
    order_id         UUID        NOT NULL,
    product_id       UUID        NOT NULL,
    seller_id        UUID        NOT NULL,
    buyer_id         UUID        NOT NULL,

    -- Whole stars. A 4.5-star submission is a UI affordance, not a
    -- measurement, and fractions here would make the distribution
    -- uncountable.
    product_rating   INTEGER     NOT NULL CHECK (product_rating BETWEEN 1 AND 5),

    -- Nullable on purpose. A buyer who only wanted to rate the item has not
    -- given the seller nought stars, and a default would put a number nobody
    -- chose into the seller's average.
    seller_rating    INTEGER     CHECK (seller_rating BETWEEN 1 AND 5),

    title            VARCHAR(200),
    body             TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One review per purchase per product, enforced here rather than only in
    -- the handler. Two concurrent submissions would both pass an
    -- application-level count and both insert; only the database can decide
    -- this one.
    --
    -- Keyed on the purchase, not the product: a buyer who orders the same
    -- thing again has a second genuine experience of it, and collapsing them
    -- would silently block anyone restocking a consumable -- the buyer whose
    -- repeat opinion is worth most.
    CONSTRAINT uq_review_per_purchase_product
        UNIQUE (seller_order_id, product_id)
);

CREATE INDEX IF NOT EXISTS ix_reviews_product ON reviews (product_id);
CREATE INDEX IF NOT EXISTS ix_reviews_seller  ON reviews (seller_id);
CREATE INDEX IF NOT EXISTS ix_reviews_buyer   ON reviews (buyer_id);

-- Rule 3: nothing publishes to Kafka directly. A review changing a product's
-- average is a read-model concern, and the projection carrying it there must
-- be driven by the same transaction that wrote the review.
CREATE TABLE IF NOT EXISTS outbox_messages (
    id             UUID PRIMARY KEY,
    aggregate_type VARCHAR(255) NOT NULL,
    aggregate_id   VARCHAR(255) NOT NULL,
    type           VARCHAR(255) NOT NULL,
    payload        JSONB        NOT NULL,
    created_at     TIMESTAMPTZ  DEFAULT NOW(),
    processed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_outbox_unprocessed
    ON outbox_messages (created_at) WHERE processed_at IS NULL;

-- Rule 4: a retried submission must not become a second review.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        VARCHAR(255) PRIMARY KEY,
    result_id  UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
