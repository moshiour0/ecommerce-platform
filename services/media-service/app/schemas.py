from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MediaRegisterRequest(BaseModel):
    owner_id: UUID
    # Defaults to a listing photo, which is everything this service has
    # stored so far. The other purposes exist for the Media Center.
    purpose: str = "product_image"
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
    purpose: str
    # Derived, never stored. Callers must not infer servability by comparing
    # the status string themselves -- that is how a new status ends up
    # accidentally treated as safe.
    #
    # `servable` means scanned clean. `publicly_servable` additionally means
    # not confidential: a KYC document that passes a virus scan is still a
    # KYC document.
    servable: bool
    publicly_servable: bool = False
    scan_detail: Optional[str] = None


class MediaLocationResponse(BaseModel):
    id: UUID
    storage_key: str
    # Served straight from the object store: no service is in the path of
    # the bytes.
    url: str


class UploadUrlResponse(BaseModel):
    url: str
    method: str
    headers: dict
    expires_in: int
    storage_key: str
