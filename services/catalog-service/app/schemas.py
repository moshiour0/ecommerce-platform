from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from uuid import UUID
from datetime import datetime

class CategoryBase(BaseModel):
    name: str
    description: Optional[str] = None

class CategoryCreate(CategoryBase):
    pass

class CategoryResponse(CategoryBase):
    id: UUID
    model_config = ConfigDict(from_attributes=True)

class ProductBase(BaseModel):
    # Optional at the edge and defaulted to the platform seller, which is a
    # real active seller in seller_db rather than an exemption (migration 015).
    # Stored NOT NULL, and checked against seller-service before the product is
    # created -- a seller who may not sell cannot list, whichever id is used.
    #
    # Returned on reads as well as accepted on writes: bff-checkout reads it to
    # attribute every order line to a seller, and order-saga refuses a checkout
    # with a line that has none.
    seller_id: Optional[UUID] = None
    category_id: UUID
    # Required. It was silently dropped before -- pydantic discards unknown
    # fields, so callers sent a sku, got a 201, and the product was stored
    # and indexed without one. Every caller in this repository already
    # sends it.
    sku: str = Field(..., min_length=2, max_length=64)
    name: str
    description: Optional[str] = None
    price_cents: int = Field(..., ge=0)
    # Accepted and stored now. The default is what made this invisible: a
    # response built from an ORM row with no such attribute fell back to
    # True, so is_active=false was echoed back as true.
    is_active: bool = True

class ProductCreate(ProductBase):
    pass

class ProductResponse(ProductBase):
    id: UUID
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
