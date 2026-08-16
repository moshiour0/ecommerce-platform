import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .database import Base

class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), nullable=False, index=True, unique=True)
    quantity_available = Column(Integer, nullable=False, default=0)
    quantity_reserved = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class InventoryReservation(Base):
    """What a given order is holding, so it can be given back.

    Neither inventory_items nor order_saga_states recorded this, which is why
    ReleaseInventoryCommand could not be implemented and the dispatcher
    acknowledged it to itself instead. See migration 011.
    """

    __tablename__ = "inventory_reservations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Text: the saga's order id arrives as a string, and a compensation must
    # never fail on a cast.
    order_id = Column(String(255), nullable=False, index=True)
    product_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="held")
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    released_at = Column(DateTime(timezone=True), nullable=True)


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
