import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import InventoryItem, OutboxMessage, IdempotencyKey
from ..schemas import ReserveRequest
from .reservation_rules import (
    ReservationOutcome, StockLevel, is_lock_contention, plan_reservation,
)

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

    # Query inventory item with pessimistic locking. Every buyer of one
    # product contends for this single row, so lock_timeout (Rule 11, 3s)
    # fires routinely under flash-sale load. That is the item being busy, not
    # the service being broken, and it must not surface as a 500: a 500 tells
    # the dispatcher to retry, adding load to an already-contended row, and
    # looks identical to a crash on a dashboard.
    try:
        result = await db.execute(
            select(InventoryItem)
            .where(InventoryItem.product_id == request.product_id)
            .with_for_update()
        )
    except Exception as exc:
        if not is_lock_contention(exc):
            raise
        await db.rollback()
        logger.warning(
            f"Lock contention reserving product={request.product_id}; "
            f"answering 409 so the caller can retry"
        )
        raise HTTPException(
            status_code=409,
            detail="Inventory row is contended; retry shortly",
        )
    inventory_item = result.scalar_one_or_none()

    # The decision and the arithmetic live in reservation_rules, tested
    # without a database. This function keeps the pessimistic row lock, which
    # is what actually serialises concurrent buyers, and the transaction.
    plan = plan_reservation(
        StockLevel(inventory_item.quantity_available, inventory_item.quantity_reserved)
        if inventory_item else None,
        request.quantity,
    )

    if plan.outcome is ReservationOutcome.NOT_FOUND:
        # No auto-seed. Inventing stock for an unknown product is the backdoor
        # that made a flash sale impossible to sell out.
        raise HTTPException(status_code=404, detail=plan.detail)

    if plan.outcome is ReservationOutcome.INVALID:
        raise HTTPException(status_code=400, detail=plan.detail)

    if plan.outcome is ReservationOutcome.INSUFFICIENT:
        # Out-of-stock is a business outcome, not a transport error. Emitting
        # a durable event in the same transaction (Rule 3) is what lets the
        # saga compensate immediately instead of waiting out the reaper.
        db.add(OutboxMessage(
            aggregate_type="Inventory",
            aggregate_id=str(inventory_item.product_id),
            type=plan.event,
            payload={
                "id": str(inventory_item.id),
                "product_id": str(inventory_item.product_id),
                "quantity_requested": request.quantity,
                "quantity_available": inventory_item.quantity_available,
                "reason": "InsufficientStock",
                "occurred_at": datetime.now(timezone.utc).isoformat()
            }
        ))

        # Bind the key to this item so a retry replays the same rejection
        # instead of falling into the "processed but no result" path.
        idem_result = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
        )
        idem_record = idem_result.scalar_one()
        idem_record.result_id = inventory_item.id

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=str(e))

        raise HTTPException(status_code=409, detail=plan.detail)

    # Apply the planned levels rather than recomputing them, so the numbers
    # the tests assert are the numbers that get written.
    inventory_item.quantity_available = plan.new_level.quantity_available
    inventory_item.quantity_reserved = plan.new_level.quantity_reserved

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
