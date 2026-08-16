from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AuditAppendRequest(BaseModel):
    actor: str = Field(..., min_length=1, max_length=255)
    action: str = Field(..., min_length=1, max_length=255)
    resource_type: str = Field(..., min_length=1, max_length=128)
    resource_id: str = Field(..., min_length=1, max_length=255)
    payload: Dict[str, Any] = Field(default_factory=dict)
    # When the event happened. Defaults to now at append time, but a caller
    # replaying history -- the DLQ reprocessor escalating an old failure --
    # should send the original time, since it is what gets hashed.
    recorded_at: Optional[datetime] = None


class AuditRecordResponse(BaseModel):
    sequence: int
    actor: str
    action: str
    resource_type: str
    resource_id: str
    payload: Dict[str, Any]
    recorded_at: datetime
    prev_hash: str
    record_hash: str


class ChainBreakResponse(BaseModel):
    index: int
    sequence: Optional[int] = None
    reason: str


class VerifyResponse(BaseModel):
    records_checked: int
    intact: bool
    breaks: List[ChainBreakResponse]
