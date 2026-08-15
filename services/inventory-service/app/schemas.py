from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime

class ReserveRequest(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)

class InventoryResponse(BaseModel):
    id: UUID
    product_id: UUID
    quantity_available: int
    quantity_reserved: int
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
