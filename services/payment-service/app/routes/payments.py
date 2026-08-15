from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import ChargeRequest, PaymentResponse, RefundRequest, RefundResponse
from ..services.payment_service import process_charge, process_refund

router = APIRouter(prefix="/payments", tags=["payments"])

@router.post("/charge", response_model=PaymentResponse, status_code=201)
async def charge_payment_endpoint(
    request: ChargeRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await process_charge(db, request, idempotency_key)


@router.post("/refund", response_model=RefundResponse, status_code=200)
async def refund_payment_endpoint(
    request: RefundRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    """
    Compensating endpoint for RefundPaymentCommand.

    Returns 200 whether or not a charge existed: refunding an uncharged order
    is a legitimate no-op, not a client error. Returning 4xx here would stall
    the saga at TIMED_OUT with no way to reach ROLLBACK_COMPLETED.
    """
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    return await process_refund(db, request, idempotency_key)
