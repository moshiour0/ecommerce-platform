from pydantic import BaseModel, ConfigDict
from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any

class FraudRequest(BaseModel):
    user_id: UUID
    ip_address: Optional[str] = None
    device_fingerprint: Optional[str] = None
    telemetry: Optional[Dict[str, Any]] = None

class FraudResponse(BaseModel):
    id: UUID
    user_id: UUID
    ip_address: Optional[str] = "127.0.0.1"
    device_fingerprint: Optional[str] = "unknown"
    risk_score: int
    is_blocked: bool
    reason: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)