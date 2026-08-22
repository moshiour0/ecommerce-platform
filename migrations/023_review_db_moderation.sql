-- Review moderation: reporting and takedown.
--
-- Deliberately not automated text classification. A profanity or abuse model
-- for a marketplace operating in Bengali and English is a wordlist somebody
-- has to be accountable for, and inventing one here would ship a judgement
-- with a confident face and no author. The signal that actually exists is
-- people reporting things, so that is what this uses.
--
-- The design pressure runs both ways. Hide on a single report and any seller
-- can silence a one-star review with one click, which turns the review system
-- into a formality. Never hide automatically and genuinely abusive content
-- stays up until a human happens to look. Hence several *distinct* reporters,
-- and a human ruling that no volume of further reports can overturn.

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS moderation_state VARCHAR(32)
        NOT NULL DEFAULT 'visible',
    ADD COLUMN IF NOT EXISTS moderated_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS moderation_note  TEXT;

-- Public reads filter on this, and so do the aggregates. The same predicate
-- for both is deliberate: a review hidden from the page but still counted in
-- the average is worse than either, because the rating moves for a reason
-- nobody can see.
CREATE INDEX IF NOT EXISTS ix_reviews_public
    ON reviews (product_id)
    WHERE moderation_state IN ('visible', 'cleared');

CREATE INDEX IF NOT EXISTS ix_reviews_moderation_queue
    ON reviews (moderation_state, created_at)
    WHERE moderation_state = 'hidden_pending_review';

CREATE TABLE IF NOT EXISTS review_reports (
    id          UUID PRIMARY KEY,
    review_id   UUID        NOT NULL,
    reporter_id UUID        NOT NULL,
    reason      VARCHAR(32) NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One report per person per review, enforced here rather than only in the
    -- handler. The threshold counts *distinct reporters*, so without this
    -- constraint one determined person could hide any review by filing three
    -- times -- which is precisely the abuse the threshold exists to prevent.
    CONSTRAINT uq_one_report_per_person UNIQUE (review_id, reporter_id)
);

CREATE INDEX IF NOT EXISTS ix_review_reports_review
    ON review_reports (review_id);
