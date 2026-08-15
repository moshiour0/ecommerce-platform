from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..schemas import PromotionCreate, PromotionResponse
from ..services.promotion_service import create_promotion

router = APIRouter(prefix="/promotions", tags=["promotions"])

@router.post("/", response_model=PromotionResponse, status_code=201)
async def create_promotion_endpoint(
    promo_in: PromotionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await create_promotion(db, promo_in, idempotency_key)
