from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Any, Dict, List, Union

class CreateOrderRequest(BaseModel):
    user_id: UUID
    total_cents: int = Field(..., ge=1)
    # The BFF sends a List, but we allow Dict for backward compatibility with older tests
    items_payload: Union[List[Any], Dict[str, Any]]

class SagaEventRequest(BaseModel):
    event_type: str
    payload: Dict[str, Any]

class SagaResponse(BaseModel):
    id: UUID
    user_id: UUID
    status: str
    total_cents: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)