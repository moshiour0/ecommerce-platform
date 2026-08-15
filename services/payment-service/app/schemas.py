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

class RefundRequest(BaseModel):
    order_id: UUID
    # Required so a no-op reversal can still be recorded when no charge exists.
    # The saga reaper's compensation payload always carries user_id.
    user_id: UUID
    reason: Optional[str] = "SagaCompensation"


class RefundResponse(BaseModel):
    order_id: UUID
    refunded: bool
    refunded_cents: int
    status: str
    detail: str


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    user_id: UUID
    amount_cents: int
    currency: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)