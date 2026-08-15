from pydantic import BaseModel, ConfigDict
from uuid import UUID
from datetime import datetime
from typing import Dict, Any

class NotificationRequest(BaseModel):
    user_id: UUID
    channel: str
    template_name: str
    payload: Dict[str, Any]

class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    channel: str
    template_name: str
    status: str
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
