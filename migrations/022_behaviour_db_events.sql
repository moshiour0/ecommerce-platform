-- Behaviour: the last inert term of the ranking formula.
--
-- ARCHITECTURE §3g scores products as
--     text_relevance x quality_boost x distance_decay x personalisation
-- and `affinity` has been passed as None since the day that was written,
-- because nothing recorded what a buyer had looked at. None means exactly 1.0,
-- so the term has been correct and idle.
--
-- This table holds the most personal data on the platform -- what individual
-- people looked at -- which is why it is a database of its own, why affinity
-- is exposed as normalised weights rather than as the events behind them, and
-- why there is a retention sweep rather than an intention to add one.

CREATE TABLE IF NOT EXISTS behaviour_events (
    id           UUID PRIMARY KEY,

    buyer_id     UUID        NOT NULL,

    -- view / cart_add / purchase. Text rather than an enum so adding a kind is
    -- a deploy of one service: affinity_rules skips a kind it does not
    -- recognise rather than failing on it.
    kind         VARCHAR(32) NOT NULL,

    -- References into other services' data. No foreign keys (Rule 1), and
    -- captured at the time of the interaction rather than joined later: a
    -- product can be recategorised or change hands, and what the ranking wants
    -- is what the buyer was interested in *then*.
    product_id   UUID,
    category_id  UUID,
    seller_id    UUID,

    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The only query this table serves: one buyer's recent history.
CREATE INDEX IF NOT EXISTS ix_behaviour_buyer_time
    ON behaviour_events (buyer_id, occurred_at DESC);

-- For the retention sweep, which is not scoped to a buyer.
CREATE INDEX IF NOT EXISTS ix_behaviour_occurred
    ON behaviour_events (occurred_at);

-- No outbox and no idempotency_keys table here, unlike every other service,
-- and the omission is deliberate. A lost behaviour event costs a little
-- ranking signal; a duplicated one costs slightly more weight on an interest
-- the buyer really does have. Neither is a correctness failure, and paying
-- outbox machinery for a page view would be pricing this like money.
