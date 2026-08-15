from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import DispatchRequest, DispatchResponse
from ..services.fulfillment_service import dispatch_order

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

@router.post("", response_model=DispatchResponse, status_code=201)
async def dispatch_order_endpoint(
    request: DispatchRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await dispatch_order(db, request, idempotency_key)
