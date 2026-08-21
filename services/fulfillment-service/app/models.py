import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .database import Base

class FulfillmentRecord(Base):
    __tablename__ = "fulfillment_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    destination_region = Column(String(255), nullable=False)
    tracking_number = Column(String(255), nullable=False)
    courier = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Shipment(Base):
    """One parcel: which seller order, which courier, and its last status."""

    __tablename__ = "shipments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    provider = Column(String(64), nullable=False)
    tracking_code = Column(String(128), nullable=True)

    status = Column(String(32), nullable=False, default="PICKUP_PENDING")

    # What the courier actually said. Kept verbatim because an unmapped status
    # is exactly the case where `status` is stale and this is the only record
    # of what happened.
    raw_status = Column(String(128), nullable=True)
    raw_status_at = Column(DateTime(timezone=True), nullable=True)
    unmapped = Column(Boolean, nullable=False, default=False)

    # What the courier is expected to collect at the door, copied at creation:
    # the amount owed is what was agreed when the parcel went out, not what the
    # order says weeks later.
    cod_amount_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="BDT")

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class SettlementRow(Base):
    """One line of a courier's remittance file, reconciled or not.

    Rows that did not reconcile are stored too: a rejected row that is
    forgotten is a dispute nobody can reconstruct.
    """

    __tablename__ = "settlement_rows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(64), nullable=False)
    batch_reference = Column(String(128), nullable=False)
    row_reference = Column(String(128), nullable=False)

    seller_order_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    expected_cents = Column(Integer, nullable=False, default=0)
    collected_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="BDT")

    outcome = Column(String(32), nullable=False)
    detail = Column(String(1024), nullable=True)

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
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
