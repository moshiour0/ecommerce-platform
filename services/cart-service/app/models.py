import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .database import Base

class CartState(Base):
    __tablename__ = "cart_state"

    cart_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    items = Column(JSONB, nullable=False, default=dict)
    status = Column(String(50), nullable=False, default='active')
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # When this row last changed. The sweeper's reclaim pass measures how
    # long a cart has sat in checkout_in_progress from here; without it that
    # pass filtered on a column that did not exist and never ran.
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

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
    result_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
