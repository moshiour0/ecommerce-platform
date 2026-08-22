import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID

from .database import Base


def _now():
    return datetime.now(timezone.utc)


class BehaviourEventRow(Base):
    """One thing a buyer did, and the context it happened in.

    Recorded at the point of the interaction with the category and seller the
    caller already had on screen, rather than joined back later. That is not
    laziness about normalisation: a product can be recategorised or change
    hands, and what the ranking wants to know is what the buyer was interested
    in *at the time*, not what that product became afterwards.

    No outbox and no idempotency key, deliberately. A lost behaviour event
    costs a little ranking signal; a duplicated one costs slightly more weight
    on an interest the buyer really does have. Neither is a correctness
    failure, and paying outbox machinery for a page view would be pricing this
    like money.
    """

    __tablename__ = "behaviour_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    buyer_id = Column(UUID(as_uuid=True), nullable=False)

    # view / cart_add / purchase. A string rather than an enum so that adding a
    # kind is a deploy of this service alone -- affinity_rules skips a kind it
    # does not recognise instead of failing on it.
    kind = Column(String(32), nullable=False)

    # References into other services' data, no foreign keys (Rule 1).
    product_id = Column(UUID(as_uuid=True), nullable=True)
    category_id = Column(UUID(as_uuid=True), nullable=True)
    seller_id = Column(UUID(as_uuid=True), nullable=True)

    occurred_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        # The only query this table serves: one buyer's recent history. Ordered
        # by time because the retention sweep and the profile build both walk
        # it that way.
        Index("ix_behaviour_buyer_time", "buyer_id", "occurred_at"),
        # For the retention sweep, which is not scoped to a buyer.
        Index("ix_behaviour_occurred", "occurred_at"),
    )
