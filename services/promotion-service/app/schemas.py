from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Optional

class PromotionBase(BaseModel):
    code: str
    discount_percent: int = Field(..., ge=1, le=100)
    max_discount_cents: int = Field(..., ge=0)
    is_active: bool = True
    expires_at: Optional[datetime] = None

class PromotionCreate(PromotionBase):
    pass

class PromotionResponse(PromotionBase):
    id: UUID
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
