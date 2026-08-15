from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from ..database import get_db, get_redis
from ..schemas import CartAddRequest, CartResponse
from ..services.cart_service import add_to_cart, checkout_cart, get_cart
import uuid

router = APIRouter(prefix="/cart", tags=["cart"])

@router.post("/{user_id}/items", response_model=CartResponse, status_code=200)
async def add_item_endpoint(
    user_id: uuid.UUID,
    request: CartAddRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await add_to_cart(db, redis, str(user_id), request, idempotency_key)

@router.post("/{user_id}/checkout", status_code=202)
async def checkout_endpoint(
    user_id: uuid.UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis)
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        
    return await checkout_cart(db, redis, str(user_id), idempotency_key)

@router.get("/{user_id}", response_model=CartResponse, status_code=200)
async def get_cart_endpoint(
    user_id: uuid.UUID,
    redis: Redis = Depends(get_redis)
):
    return await get_cart(redis, str(user_id))
