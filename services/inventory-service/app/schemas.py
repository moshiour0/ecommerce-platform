from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
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
    # Which of the order's lines to act on. Absent means all of them, which is
    # what the saga reaper wants -- it compensates a whole order blind.
    #
    # A marketplace order is split across sellers, and each seller's part
    # cancels, delivers and returns on its own schedule. Releasing the whole
    # order because one seller cancelled would put another seller's still-live
    # stock back on sale.
    product_ids: Optional[List[str]] = None


class ConsumeRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=255)
    product_ids: Optional[List[str]] = None


class ConsumeResponse(BaseModel):
    order_id: str
    consumed: int          # units retired from reserved, not returned
    reservations: int      # ledger rows settled
    detail: str


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
