from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from uuid import UUID
from datetime import datetime

class ReserveRequest(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)
    # Optional so a caller with no saga -- the contention check, a manual
    # reservation -- still works. When present the hold is recorded against the
    # order and becomes releasable; without it the units are reserved but
    # nothing can give them back, which is the state the whole platform was in.
    order_id: Optional[str] = Field(None, max_length=255)


class ReleaseRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=255)


class ReleaseResponse(BaseModel):
    order_id: str
    released: int          # units returned to available
    reservations: int      # ledger rows settled
    detail: str

class InventoryResponse(BaseModel):
    id: UUID
    product_id: UUID
    quantity_available: int
    quantity_reserved: int
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
