from pydantic import BaseModel, ConfigDict
from uuid import UUID
from datetime import datetime

class DispatchRequest(BaseModel):
    order_id: UUID
    destination_region: str

class DispatchResponse(BaseModel):
    id: UUID
    order_id: UUID
    destination_region: str
    tracking_number: str
    courier: str
    status: str
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
