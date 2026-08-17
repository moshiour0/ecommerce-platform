import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB

from .database import Base
from .services.media_rules import AssetPurpose, MediaStatus


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    content_type = Column(String(128), nullable=False)
    size_bytes = Column(Integer, nullable=False)

    # Where the bytes live. This service owns metadata and lifecycle only --
    # media_meta_db, not media_db -- so the key is opaque here.
    storage_key = Column(String(1024), nullable=True)
    checksum = Column(String(128), nullable=True)

    # What the asset is for. Decides who may read it, which is why it is not
    # nullable: an asset nobody has classified would have to be treated as
    # confidential, and that is a worse default to discover at read time.
    purpose = Column(String(32), nullable=False,
                     default=AssetPurpose.PRODUCT_IMAGE.value, index=True)

    # Quarantined on arrival, and only a completed clean scan changes that.
    # The default lives here as well as in the rules so a row inserted by any
    # other path is still born un-servable.
    status = Column(String(32), nullable=False,
                    default=MediaStatus.QUARANTINED.value, index=True)
    scan_detail = Column(String(1024), nullable=True)

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
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
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key = Column(String(255), primary_key=True)
    result_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
