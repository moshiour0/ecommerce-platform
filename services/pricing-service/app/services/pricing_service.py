import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import Price, OutboxMessage, IdempotencyKey
from ..schemas import PriceSetRequest

async def set_product_price(db: AsyncSession, price_in: PriceSetRequest, idempotency_key: str) -> Price:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")
    
    price_id = uuid.uuid4()
    price_record = Price(
        id=price_id,
        product_id=price_in.product_id,
        base_price_cents=price_in.base_price_cents,
        currency=price_in.currency,
        effective_date=datetime.now(timezone.utc)
    )
    db.add(price_record)

    # Create OutboxMessage within the same atomic transaction
    payload = {
        "id": str(price_record.id),
        "product_id": str(price_record.product_id),
        "base_price_cents": price_record.base_price_cents,
        "currency": price_record.currency,
        "effective_date": price_record.effective_date.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="Price",
        aggregate_id=str(price_record.product_id),
        type="PriceUpdated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(price_record)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return price_record
