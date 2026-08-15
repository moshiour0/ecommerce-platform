from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import uuid

from ..database import get_db
from ..schemas import PriceSetRequest, PriceResponse
from ..services.pricing_service import set_product_price
from ..models import Price  # Corrected to match your actual model

router = APIRouter(prefix="/prices", tags=["prices"])

@router.post("/", response_model=PriceResponse, status_code=201)
async def set_price_endpoint(
    price_in: PriceSetRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    return await set_product_price(db, price_in, idempotency_key)

@router.get("/{product_id}", response_model=PriceResponse, status_code=200)
async def get_price_endpoint(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    # Updated to query the Price model
    result = await db.execute(select(Price).where(Price.product_id == product_id))
    price_record = result.scalar_one_or_none()
    
    if not price_record:
        raise HTTPException(status_code=404, detail="Price not found for product")
        
    return price_record