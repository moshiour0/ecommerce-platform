import uuid
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import Product, OutboxMessage, IdempotencyKey
from ..schemas import ProductCreate
from .catalog_rules import (
    PLATFORM_SELLER_ID, InvalidSku, build_product_event, normalize_sku,
)

async def create_product(db: AsyncSession, product_in: ProductCreate, idempotency_key: str) -> Product:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    try:
        sku = normalize_sku(product_in.sku)
    except InvalidSku as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    # Create Product
    product_id = uuid.uuid4()
    product = Product(
        id=product_id,
        # Defaults to the platform seller until seller-service exists.
        seller_id=product_in.seller_id or uuid.UUID(PLATFORM_SELLER_ID),
        category_id=product_in.category_id,
        sku=sku,
        name=product_in.name,
        description=product_in.description,
        price_cents=product_in.price_cents,
        is_active=product_in.is_active
    )
    db.add(product)

    # Create OutboxMessage. Built from the stored row by build_product_event,
    # which fails loudly if a field the read model needs is missing -- this
    # payload used to omit sku and is_active entirely, so every indexed
    # product had a null SKU and was active whatever the caller asked for.
    payload = build_product_event(
        product, datetime.now(timezone.utc).isoformat())
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
    except IntegrityError as e:
        await db.rollback()
        # sku is unique now, so a duplicate is an ordinary client mistake and
        # must not be reported as a server fault: a 500 tells the caller to
        # retry, and retrying a duplicate SKU fails identically forever.
        if "ux_products_sku" in str(e.orig) or "sku" in str(e.orig):
            raise HTTPException(
                status_code=409,
                detail=f"sku {sku!r} already exists")
        # Anything else is a genuine constraint problem -- an unknown
        # category_id, most likely -- which is also the caller's, not ours.
        raise HTTPException(status_code=422, detail=str(e.orig))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return product
