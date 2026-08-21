from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

class CreateOrderRequest(BaseModel):
    user_id: UUID
    total_cents: int = Field(..., ge=1)
    # The BFF sends a List, but we allow Dict for backward compatibility with older tests
    items_payload: Union[List[Any], Dict[str, Any]]

class SellerOrderActionRequest(BaseModel):
    """The body of a lifecycle transition.

    All optional: `confirm` and `settle` need nothing, `dispatch` takes the
    courier, and `cancel` and `mark_rto` require a reason -- enforced by
    cod_rules rather than here, so the refusal explains itself instead of
    being a 422 about a field.
    """
    reason: Optional[str] = None
    courier_name: Optional[str] = None
    tracking_code: Optional[str] = None


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