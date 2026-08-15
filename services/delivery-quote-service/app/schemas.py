from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID, uuid4
from datetime import datetime

class QuoteRequest(BaseModel):
    # Auto-generate a UUID if the BFF sends undefined/null
    cart_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    destination_region: str
    weight_grams: int

class QuoteResponse(BaseModel):
    id: UUID
    cart_id: UUID
    user_id: UUID
    destination_region: str
    amount_cents: int
    estimated_days: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)