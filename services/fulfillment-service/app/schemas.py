from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
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


class ShipmentCreateRequest(BaseModel):
    seller_order_id: UUID
    order_id: UUID
    provider: str = Field(..., min_length=1, max_length=64)
    tracking_code: Optional[str] = Field(None, max_length=128)
    # What the courier collects at the door. Copied onto the shipment rather
    # than looked up later, because that is the figure the settlement file is
    # reconciled against.
    cod_amount_cents: int = Field(0, ge=0)
    currency: str = Field("BDT", min_length=3, max_length=3)


class CourierCallbackRequest(BaseModel):
    """What a courier pushes when a parcel moves.

    `status` is the courier's own word, untranslated. Mapping it is this
    service's job and nobody else's -- a caller that could send a canonical
    status could send DELIVERED for a parcel that never arrived.
    """
    tracking_code: Optional[str] = Field(None, max_length=128)
    seller_order_id: Optional[UUID] = None
    status: str = Field(..., min_length=1, max_length=128)
    occurred_at: Optional[datetime] = None


class SettlementRowInput(BaseModel):
    reference: str = Field(..., min_length=1, max_length=128)
    collected_cents: int


class SettlementRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=64)
    # The courier's own batch id. Couriers resend whole files after a
    # correction, and this is what makes the second copy recognisable as the
    # same batch rather than as new money.
    batch_reference: str = Field(..., min_length=1, max_length=128)
    rows: List[SettlementRowInput]


class ShipmentResponse(BaseModel):
    id: UUID
    seller_order_id: UUID
    order_id: UUID
    provider: str
    tracking_code: Optional[str] = None
    status: str
    raw_status: Optional[str] = None
    unmapped: bool = False
    cod_amount_cents: int
    currency: str


class CallbackResponse(BaseModel):
    shipment_id: UUID
    provider: str
    raw_status: str
    canonical_status: Optional[str] = None
    # Which seller-order action this drove, if any. Null for an informational
    # update and for an unmapped word.
    action: Optional[str] = None
    action_result: Optional[str] = None
    unmapped: bool = False
