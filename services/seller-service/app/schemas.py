from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class SellerRegisterRequest(BaseModel):
    legal_name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    contact_email: str = Field(..., min_length=3, max_length=320)
    contact_phone: Optional[str] = Field(None, max_length=32)

    address_line: Optional[str] = Field(None, max_length=512)
    city: Optional[str] = Field(None, max_length=128)
    district: Optional[str] = Field(None, max_length=128)
    country: str = Field("BD", min_length=2, max_length=2)
    latitude: Optional[str] = Field(None, max_length=32)
    longitude: Optional[str] = Field(None, max_length=32)


class DocumentRef(BaseModel):
    """One KYC document, by reference.

    media_id points at an asset registered with media-service under
    purpose=seller_document. This service never sees the bytes and stores
    nothing from inside the document.
    """
    document_type: str = Field(..., max_length=32)
    media_id: UUID


class DocumentSubmissionRequest(BaseModel):
    documents: List[DocumentRef] = Field(..., min_length=1)


class ReviewResultRequest(BaseModel):
    approved: bool
    # Required when approved is false; the rules enforce that rather than the
    # schema, so the refusal carries an explanation instead of a 422 about a
    # field.
    reason: Optional[str] = Field(None, max_length=1024)


class ContractAcceptanceRequest(BaseModel):
    version: int


class ReasonRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1024)


class SellerResponse(BaseModel):
    id: UUID
    legal_name: str
    display_name: str
    contact_email: str
    contact_phone: Optional[str] = None
    status: str
    status_reason: Optional[str] = None

    accepted_contract_version: Optional[int] = None
    current_contract_version: int

    # Derived, never stored. Callers must not compare the status string
    # themselves -- that is how a new status ends up accidentally treated as
    # permission to sell.
    may_list_products: bool
    may_receive_orders: bool
    needs_contract_acceptance: bool

    # What is still missing before a reviewer can look at them. Empty for a
    # seller past that point, and the reason a dashboard can be specific
    # instead of saying "incomplete".
    missing_documents: List[str] = []

    city: Optional[str] = None
    district: Optional[str] = None
    country: str = "BD"

    created_at: Optional[datetime] = None


class SellerPermissionResponse(BaseModel):
    """The narrow answer other services need.

    catalog-service does not need a seller's address or their rejection
    reason; it needs to know whether this id may put a product on the
    platform. A small response is a small blast radius.
    """
    seller_id: UUID
    status: str
    may_list_products: bool
    may_receive_orders: bool
