from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import FraudRequest, FraudResponse
from ..services.fraud_service import evaluate_checkout_risk

router = APIRouter(prefix="/fraud", tags=["fraud"])

@router.post("/evaluate", response_model=FraudResponse, status_code=200)
async def evaluate_fraud_endpoint(
    request: FraudRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await evaluate_checkout_risk(db, request, idempotency_key)
