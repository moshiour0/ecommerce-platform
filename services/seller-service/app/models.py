import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB

from .database import Base
from .services.seller_rules import SellerStatus


class Seller(Base):
    __tablename__ = "sellers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The person or company behind the shop, as it appears on the trade
    # licence. Distinct from display_name, which is what buyers see and which
    # a seller may change; legal_name is what a document is checked against.
    legal_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    contact_email = Column(String(320), nullable=False)
    contact_phone = Column(String(32), nullable=True)

    # Registered unable to sell. The default lives here as well as in the
    # rules, so a row inserted by any other path -- a migration, a fixture, a
    # future admin tool -- is still born without permission.
    status = Column(String(32), nullable=False,
                    default=SellerStatus.REGISTERED.value, index=True)

    # Why the seller is in this status, when the transition carried a reason:
    # the rejection text, the suspension cause, the ban. Shown to the seller,
    # so it is written to be read by one.
    status_reason = Column(String(1024), nullable=True)

    # The commission contract version this seller accepted, and when. NULL
    # until they accept: an approved seller who has not accepted anything is
    # the normal state between review and going live.
    accepted_contract_version = Column(Integer, nullable=True)
    contract_accepted_at = Column(DateTime(timezone=True), nullable=True)

    # Where the shop is. Nullable because "shops near me" is a later feature
    # (roadmap §3.4) and blocking registration on coordinates a seller may not
    # know would cost sign-ups now for a feature that does not exist yet.
    # Stored as text and a lat/lon pair rather than PostGIS: the ranking
    # pipeline will own proximity, and this is the source it will read.
    address_line = Column(String(512), nullable=True)
    city = Column(String(128), nullable=True)
    district = Column(String(128), nullable=True)
    country = Column(String(2), nullable=False, default="BD")
    latitude = Column(String(32), nullable=True)
    longitude = Column(String(32), nullable=True)

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class SellerDocument(Base):
    """A KYC document, by reference only.

    The bytes are in object storage, registered with media-service under
    purpose=seller_document, which is confidential: media-service refuses to
    serve a location for it even after a clean virus scan. What lives here is
    the media id and what the seller says it is.

    Nothing about the document's *content* is stored -- no national ID number,
    no licence number, no bank account. Onboarding collects those on paper and
    the paper lives in the object store; copying the numbers into a second
    database would put the most sensitive data the platform holds in the least
    protected place.
    """
    __tablename__ = "seller_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    document_type = Column(String(32), nullable=False)

    # media-service's id for the asset. No foreign key: it is another service's
    # database (Rule 1).
    media_id = Column(UUID(as_uuid=True), nullable=False)

    submitted_at = Column(DateTime(timezone=True),
                          default=lambda: datetime.now(timezone.utc))

    # One current document of each type per seller. A resubmission replaces
    # rather than accumulates, so "which trade licence did the reviewer look
    # at" always has one answer.
    __table_args__ = (
        UniqueConstraint("seller_id", "document_type",
                         name="uq_seller_document_type"),
    )


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
