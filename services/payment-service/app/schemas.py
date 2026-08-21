from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from datetime import datetime
from typing import Optional

class ChargeRequest(BaseModel):
    order_id: UUID
    user_id: UUID
    amount_cents: int = Field(..., ge=1)
    payment_token: Optional[str] = "internal_saga_bypass"
    currency: Optional[str] = "USD"

class RefundRequest(BaseModel):
    order_id: UUID
    # Required so a no-op reversal can still be recorded when no charge exists.
    # The saga reaper's compensation payload always carries user_id.
    user_id: UUID
    reason: Optional[str] = "SagaCompensation"


class RefundResponse(BaseModel):
    order_id: UUID
    refunded: bool
    refunded_cents: int
    status: str
    detail: str


class PaymentResponse(BaseModel):
    id: UUID
    order_id: UUID
    user_id: UUID
    amount_cents: int
    currency: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EscrowDeliveryRequest(BaseModel):
    """The courier collected at the door.

    `collected_cents` is what was actually taken, not what the order says. The
    two should agree and reconciliation exists for when they do not, but the
    ledger records what happened.
    """
    seller_id: UUID
    seller_order_id: UUID
    collected_cents: int
    currency: str = "BDT"


class EscrowSettlementRequest(BaseModel):
    seller_id: UUID
    seller_order_id: UUID
    remitted_cents: int
    currency: str = "BDT"


class PayoutRequest(BaseModel):
    seller_id: UUID
    amount_cents: int
    currency: str = "BDT"


class LedgerTransactionResponse(BaseModel):
    transaction_id: UUID
    reason: str
    entries: int
    seller_owed_delta: int
    detail: str = ""


class SellerBalanceResponse(BaseModel):
    seller_id: UUID
    # Positive means the platform owes the seller. The double-entry sign is
    # flipped here into ordinary language, because this is the number a seller
    # asks for and a payout run needs.
    owed_cents: int
    currency: str = "BDT"
    entries: int
