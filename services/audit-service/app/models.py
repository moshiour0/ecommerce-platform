import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .database import Base


class AuditRecord(Base):
    """One immutable entry in the audit chain.

    Nothing in this service updates or deletes a row here, and there is no
    route that could. The hash columns are what make that claim checkable
    rather than merely stated -- see services/audit_rules.py.
    """

    __tablename__ = "audit_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Position in the chain. UNIQUE is load-bearing, not decorative: it is the
    # backstop that makes a forked chain impossible even if the advisory lock
    # in audit_service is bypassed, removed, or fails to be taken. Two
    # concurrent appends that read the same tail would compute the same
    # sequence, and exactly one of them can commit.
    sequence = Column(BigInteger, nullable=False, unique=True, index=True)

    actor = Column(String(255), nullable=False, index=True)
    action = Column(String(255), nullable=False, index=True)
    resource_type = Column(String(128), nullable=False)
    resource_id = Column(String(255), nullable=False, index=True)
    payload = Column(JSONB, nullable=False)

    # Part of the hash, so it is the time the event happened as reported by the
    # caller, not the time this row was written.
    recorded_at = Column(DateTime(timezone=True), nullable=False)

    prev_hash = Column(String(64), nullable=False)
    record_hash = Column(String(64), nullable=False, unique=True)

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_type = Column(String(255), nullable=False)
    aggregate_id = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key = Column(String(255), primary_key=True)
    result_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
