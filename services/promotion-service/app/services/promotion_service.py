import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import Promotion, OutboxMessage, IdempotencyKey
from ..schemas import PromotionCreate

async def create_promotion(db: AsyncSession, promo_in: PromotionCreate, idempotency_key: str) -> Promotion:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")
    
    # Check if a promotion with this code already exists
    existing_promo = await db.execute(select(Promotion).where(Promotion.code == promo_in.code))
    if existing_promo.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Promotion code already exists")

    promo_id = uuid.uuid4()
    promo = Promotion(
        id=promo_id,
        code=promo_in.code,
        discount_percent=promo_in.discount_percent,
        max_discount_cents=promo_in.max_discount_cents,
        is_active=promo_in.is_active,
        expires_at=promo_in.expires_at
    )
    db.add(promo)

    # Create OutboxMessage within the same atomic transaction
    payload = {
        "id": str(promo.id),
        "code": promo.code,
        "discount_percent": promo.discount_percent,
        "max_discount_cents": promo.max_discount_cents,
        "is_active": promo.is_active,
        "expires_at": promo.expires_at.isoformat() if promo.expires_at else None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="Promotion",
        aggregate_id=str(promo.id),
        type="PromotionCreated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(promo)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return promo
