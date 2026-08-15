import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import DeliveryQuote, OutboxMessage, IdempotencyKey
from ..schemas import QuoteRequest

async def generate_quote(db: AsyncSession, request: QuoteRequest, idempotency_key: str) -> DeliveryQuote:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    # Base quote simulation
    amount_cents = 1500
    estimated_days = 3

    # GEOGRAPHIC ROUTING LOGIC (Blatten hazard check)
    if request.destination_region == "Blatten":
        amount_cents += 500
        estimated_days = 7

    # Save Quote entry
    quote_id = uuid.uuid4()
    quote = DeliveryQuote(
        id=quote_id,
        cart_id=request.cart_id,
        user_id=request.user_id,
        destination_region=request.destination_region,
        amount_cents=amount_cents,
        estimated_days=estimated_days,
        created_at=datetime.now(timezone.utc)
    )
    db.add(quote)

    # Create OutboxMessage
    payload = {
        "id": str(quote.id),
        "cart_id": str(quote.cart_id),
        "user_id": str(quote.user_id),
        "destination_region": quote.destination_region,
        "amount_cents": quote.amount_cents,
        "estimated_days": quote.estimated_days,
        "created_at": quote.created_at.isoformat()
    }
    
    outbox_message = OutboxMessage(
        aggregate_type="DeliveryQuote",
        aggregate_id=str(quote.cart_id),
        type="DeliveryQuoteGenerated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(quote)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return quote
