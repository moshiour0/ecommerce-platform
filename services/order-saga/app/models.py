import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .database import Base

class OrderSagaState(Base):
    __tablename__ = "order_saga_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="PENDING")
    total_cents = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class SellerOrder(Base):
    """One seller's part of a buyer's order.

    The unit everything after checkout operates on: a courier collects from
    one seller, money is owed to one seller, and a refused delivery is one
    seller's return. See ARCHITECTURE_STATE_FINAL.md §3d.
    """
    __tablename__ = "seller_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    seller_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # The COD lifecycle. Born PENDING; nothing advances it yet.
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    status_reason = Column(String(1024), nullable=True)

    # Goods only. The parent's total_cents also carries tax, shipping and
    # promotions, which are deliberately not allocated across sellers here.
    subtotal_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="BDT")
    item_count = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class OrderLine(Base):
    """What was actually bought.

    Previously nowhere: the lines existed only inside an outbox payload on
    their way to the inventory reservation, so order_db knew a total and not
    what it was a total of.
    """
    __tablename__ = "order_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    seller_order_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    product_id = Column(UUID(as_uuid=True), nullable=False)
    seller_id = Column(UUID(as_uuid=True), nullable=False)
    quantity = Column(Integer, nullable=False)

    # The price at checkout, not a live lookup: a price that moves later must
    # not change what the buyer owes or what the seller is paid.
    price_cents = Column(Integer, nullable=False)
    line_total_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="BDT")

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type = Column(String(255), nullable=False)
    aggregate_id = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    
    key = Column(String(255), primary_key=True)
    result_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
