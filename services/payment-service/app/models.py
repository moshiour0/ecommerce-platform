import uuid
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .database import Base

class PaymentLedger(Base):
    __tablename__ = "payment_ledger"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    payment_token = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False, default="PENDING")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class LedgerEntry(Base):
    """One line of the escrow ledger.

    Append only. There is no update path and no delete path: a correction is a
    new transaction that reverses the old one, because a ledger that can be
    edited is a ledger nobody can testify from.
    """

    __tablename__ = "ledger_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Every entry of one transaction shares this. The balance check is
    # per-transaction, so without it there is no way to ask whether a
    # particular event balanced.
    transaction_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    account = Column(String(32), nullable=False)

    # Signed minor units. Positive debit, negative credit.
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="BDT")

    reason = Column(String(32), nullable=False)
    seller_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    seller_order_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # What the entry was booked at. On the entry rather than looked up later
    # because the ledger is the evidence: a seller disputing their balance is
    # owed the rate that was applied, not whatever the rate table says today.
    commission_bps = Column(Integer, nullable=True)
    contract_version = Column(Integer, nullable=True)

    detail = Column(String(512), nullable=True)
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
