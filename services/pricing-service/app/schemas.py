from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Optional

class PriceSetRequest(BaseModel):
    product_id: UUID
    base_price_cents: int = Field(..., ge=0)
    currency: Optional[str] = "USD"

class PriceResponse(BaseModel):
    id: UUID
    product_id: UUID
    base_price_cents: int
    currency: str
    effective_date: datetime
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
