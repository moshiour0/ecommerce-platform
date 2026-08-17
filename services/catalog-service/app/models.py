import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from .database import Base

class Category(Base):
    __tablename__ = "categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    description = Column(String)
    
    products = relationship("Product", back_populates="category")

class Product(Base):
    __tablename__ = "products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No foreign key: sellers are owned by another service and another
    # database, and an FK across a service boundary is the coupling Rule 1
    # forbids. category_id has one because categories genuinely live here.
    seller_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False)
    # Stock keeping unit. Unique because it identifies the product to
    # everything outside this system, and normalised to upper case on the
    # way in so "abc-1" and "ABC-1" cannot both exist.
    sku = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(String)
    price_cents = Column(Integer, nullable=False)
    # Was accepted by the API and never stored, so every product was active
    # forever and is_active=false was silently discarded.
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    category = relationship("Category", back_populates="products")

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
