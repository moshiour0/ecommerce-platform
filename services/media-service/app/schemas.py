from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MediaRegisterRequest(BaseModel):
    owner_id: UUID
    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field(..., min_length=1, max_length=128)
    size_bytes: int = Field(..., gt=0)
    storage_key: Optional[str] = Field(None, max_length=1024)
    checksum: Optional[str] = Field(None, max_length=128)


class ScanResultRequest(BaseModel):
    infected: bool
    detail: Optional[str] = Field(None, max_length=1024)


class MediaResponse(BaseModel):
    id: UUID
    owner_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    # Derived, never stored. Callers must not infer servability by comparing
    # the status string themselves -- that is how a new status ends up
    # accidentally treated as safe.
    servable: bool
    scan_detail: Optional[str] = None


class MediaLocationResponse(BaseModel):
    id: UUID
    storage_key: str
