import uuid
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.dialects.postgresql import UUID, JSONB

from .database import Base


def _now():
    return datetime.now(timezone.utc)


class Review(Base):
    """One buyer's verdict on one purchase.

    Keyed on the *purchase*, not the product: a buyer who orders the same thing
    twice has two genuine experiences of it, and collapsing them would silently
    block anyone restocking a consumable -- exactly the buyer whose repeat
    opinion is worth most.

    Both ratings live on one row because they came from one submission. Storing
    them apart would make "did this buyer rate the seller" a join, and the two
    could drift out of step with the purchase they describe.
    """

    __tablename__ = "reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # References into other services' data, with no foreign keys (Rule 1).
    seller_order_id = Column(UUID(as_uuid=True), nullable=False)
    order_id = Column(UUID(as_uuid=True), nullable=False)
    product_id = Column(UUID(as_uuid=True), nullable=False)
    seller_id = Column(UUID(as_uuid=True), nullable=False)
    buyer_id = Column(UUID(as_uuid=True), nullable=False)

    # The product rating is required; the seller rating is nullable on purpose.
    # A buyer who only wants to rate the item has not given the seller nought
    # stars, and inventing a value for them would put a number nobody chose
    # into the seller's average.
    product_rating = Column(Integer, nullable=False)
    seller_rating = Column(Integer, nullable=True)

    title = Column(String(200), nullable=True)
    body = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now,
                        nullable=False)

    __table_args__ = (
        # The verified-purchase rule, enforced by the database rather than only
        # by the handler. Two concurrent submissions for one purchase would
        # both pass an application-level check and both insert.
        UniqueConstraint("seller_order_id", "product_id",
                         name="uq_review_per_purchase_product"),
        Index("ix_reviews_product", "product_id"),
        Index("ix_reviews_seller", "seller_id"),
        Index("ix_reviews_buyer", "buyer_id"),
    )


class OutboxMessage(Base):
    """Rule 3: nothing publishes to Kafka directly.

    A review changing a product's average is a read-model concern, and the
    projection that carries it there has to be driven by the same transaction
    that wrote the review or the two can disagree.
    """

    __tablename__ = "outbox_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type = Column(String(255), nullable=False)
    aggregate_id = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    """Rule 4. A retried submission must not become a second review."""

    __tablename__ = "idempotency_keys"

    key = Column(String(255), primary_key=True)
    result_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
