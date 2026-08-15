import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import InventoryItem, OutboxMessage, IdempotencyKey
from ..schemas import ReserveRequest

logger = logging.getLogger(__name__)

async def reserve_inventory(db: AsyncSession, request: ReserveRequest, idempotency_key: str) -> InventoryItem:
    # Check idempotency — return cached result on retry (Rule 4)
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        # Key already exists — return the cached result
        existing_key = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
        )
        idem_record = existing_key.scalar_one_or_none()
        if idem_record and idem_record.result_id:
            cached_result = await db.execute(
                select(InventoryItem).where(InventoryItem.id == idem_record.result_id)
            )
            cached = cached_result.scalar_one_or_none()
            if cached:
                return cached
        raise HTTPException(status_code=409, detail="Idempotency key already processed but result not found")

    # Query inventory item with pessimistic locking
    result = await db.execute(
        select(InventoryItem)
        .where(InventoryItem.product_id == request.product_id)
        .with_for_update()
    )
    inventory_item = result.scalar_one_or_none()

    # C-2 Fix: No auto-seed. If inventory doesn't exist, return 404.
    if not inventory_item:
        raise HTTPException(status_code=404, detail="Inventory item not found for this product")

    if inventory_item.quantity_available < request.quantity:
        raise HTTPException(status_code=400, detail="Insufficient stock")

    # Update quantities
    inventory_item.quantity_available -= request.quantity
    inventory_item.quantity_reserved += request.quantity

    # Create OutboxMessage
    payload = {
        "id": str(inventory_item.id),
        "product_id": str(inventory_item.product_id),
        "quantity_reserved": request.quantity,
        "total_quantity_available": inventory_item.quantity_available,
        "total_quantity_reserved": inventory_item.quantity_reserved,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    outbox_message = OutboxMessage(
        aggregate_type="Inventory",
        aggregate_id=str(inventory_item.product_id),
        type="InventoryReserved",
        payload=payload
    )
    db.add(outbox_message)

    # Store result_id in idempotency key
    idem_result = await db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
    )
    idem_record = idem_result.scalar_one()
    idem_record.result_id = inventory_item.id

    try:
        await db.commit()
        await db.refresh(inventory_item)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return inventory_item
