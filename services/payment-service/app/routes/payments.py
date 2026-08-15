from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import ChargeRequest, PaymentResponse
from ..services.payment_service import process_charge

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
