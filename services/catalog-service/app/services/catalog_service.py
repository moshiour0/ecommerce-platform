import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import Product, OutboxMessage, IdempotencyKey
from ..schemas import ProductCreate

async def create_product(db: AsyncSession, product_in: ProductCreate, idempotency_key: str) -> Product:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    # Create Product
    product_id = uuid.uuid4()
    product = Product(
        id=product_id,
        category_id=product_in.category_id,
        name=product_in.name,
        description=product_in.description,
        price_cents=product_in.price_cents
    )
    db.add(product)

    # Create OutboxMessage
    payload = {
        "id": str(product.id),
        "category_id": str(product.category_id),
        "name": product.name,
        "description": product.description,
        "price_cents": product.price_cents,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="Product",
        aggregate_id=str(product.id),
        type="ProductCreated",
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(product)
    except Exception as e:
        await db.rollback()
        # In a real scenario, handle constraint violations separately (e.g. invalid category_id)
        raise HTTPException(status_code=500, detail=str(e))

    return product
