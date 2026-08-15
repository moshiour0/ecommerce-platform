from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Optional

class ChargeRequest(BaseModel):
    order_id: UUID
    user_id: UUID
    amount_cents: int = Field(..., ge=1)
    payment_token: Optional[str] = "internal_saga_bypass"
    currency: Optional[str] = "USD"

class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    user_id: UUID
    amount_cents: int
    currency: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)