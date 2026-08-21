import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import (
    IdempotencyKey, InventoryItem, InventoryReservation, OutboxMessage,
)
from ..schemas import ReleaseRequest, ReserveRequest
from .reservation_rules import (
    ConsumeOutcome, plan_consume,
    ReleaseOutcome, ReservationOutcome, StockLevel, is_lock_contention,
    plan_release, plan_reservation,
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

    # Record what this order is holding, in the same transaction as the units
    # moving. Without it nothing knows what to give back, which is why
    # ReleaseInventoryCommand was a no-op the dispatcher acknowledged to
    # itself. An order_id is optional -- the contention check reserves without
    # a saga -- and those units are simply not releasable by order.
    if request.order_id:
        db.add(InventoryReservation(
            order_id=request.order_id,
            product_id=inventory_item.product_id,
            quantity=request.quantity,
            status="held",
        ))

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


async def release_inventory(db: AsyncSession, request: ReleaseRequest,
                            idempotency_key: str) -> dict:
    """Return everything an order is holding. The inverse of reserve (§5).

    Idempotent by construction rather than by an idempotency key: rows are
    matched on status='held', so a second release finds nothing and succeeds
    as a no-op. That matters because the reaper issues this unconditionally --
    at INVENTORY_RESERVED it cannot know whether the reservation landed before
    the acknowledgement dropped, so both legs are compensated blind (§5).

    An order that never reserved matches no rows and is also a success. A
    release that errored on "nothing to release" would strand every saga that
    timed out before reserving.
    """
    query = (
        select(InventoryReservation)
        .where(InventoryReservation.order_id == request.order_id)
        .where(InventoryReservation.status == "held")
    )
    # Scoped to specific lines when the caller names them. One seller's part of
    # a split order cancels on its own schedule, and releasing the whole order
    # would put another seller's live stock back on sale.
    if getattr(request, "product_ids", None):
        query = query.where(
            InventoryReservation.product_id.in_(request.product_ids))
    result = await db.execute(query)
    holds = list(result.scalars().all())

    if not holds:
        logger.info("release: order %s holds nothing", request.order_id)
        return {"order_id": request.order_id, "released": 0, "reservations": 0,
                "detail": "no live reservation; nothing to return"}

    released_units = 0
    details = []

    for hold in holds:
        # Same pessimistic lock as the reserve path: releasing races with
        # buyers of the same product, and both sides must serialise on the row.
        try:
            item_result = await db.execute(
                select(InventoryItem)
                .where(InventoryItem.product_id == hold.product_id)
                .with_for_update()
            )
        except Exception as exc:
            if not is_lock_contention(exc):
                raise
            await db.rollback()
            logger.warning("release: row contended for product=%s", hold.product_id)
            raise HTTPException(status_code=409,
                                detail="Inventory row is contended; retry shortly")

        item = item_result.scalar_one_or_none()
        plan = plan_release(
            StockLevel(item.quantity_available, item.quantity_reserved)
            if item else None,
            hold.quantity,
        )

        if plan.outcome is ReleaseOutcome.INVALID:
            await db.rollback()
            raise HTTPException(status_code=400, detail=plan.detail)

        if plan.released:
            item.quantity_available = plan.new_level.quantity_available
            item.quantity_reserved = plan.new_level.quantity_reserved
            released_units += hold.quantity

            db.add(OutboxMessage(
                aggregate_type="Inventory",
                aggregate_id=str(hold.product_id),
                type=plan.event,
                payload={
                    "id": str(item.id),
                    "order_id": hold.order_id,
                    "product_id": str(hold.product_id),
                    "quantity_released": hold.quantity,
                    "total_quantity_available": item.quantity_available,
                    "total_quantity_reserved": item.quantity_reserved,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            ))

        # Settled either way: a hold against a product row that no longer
        # exists cannot be returned, and leaving it 'held' would make every
        # future release retry it forever.
        hold.status = "released"
        hold.released_at = datetime.now(timezone.utc)
        details.append(plan.detail)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("release: order %s returned %s unit(s) across %s reservation(s)",
                request.order_id, released_units, len(holds))
    return {"order_id": request.order_id, "released": released_units,
            "reservations": len(holds), "detail": "; ".join(details)}


async def consume_inventory(db: AsyncSession, request, idempotency_key: str) -> dict:
    """Retire what an order is holding, without returning it to available.

    The counterpart to release, and the asymmetry is the whole point: release
    conserves the total because the goods are back on a shelf, and consume does
    not because they are in a customer's hands.

    Nothing ever called this before, so `quantity_reserved` only ever grew.
    Reserve moved units out of available and held them; nothing moved them out
    of held. A delivered order's units stayed reserved forever, and the column
    stopped meaning "committed to open orders".

    Under COD the moment is delivery, not checkout: a dispatched parcel is
    still returnable and stays held. Idempotent by construction like release --
    rows are matched on status='held', so a retried courier callback finds
    nothing and succeeds.
    """
    query = (
        select(InventoryReservation)
        .where(InventoryReservation.order_id == request.order_id)
        .where(InventoryReservation.status == "held")
    )
    if getattr(request, "product_ids", None):
        query = query.where(
            InventoryReservation.product_id.in_(request.product_ids))
    result = await db.execute(query)
    holds = list(result.scalars().all())

    if not holds:
        logger.info("consume: order %s holds nothing", request.order_id)
        return {"order_id": request.order_id, "consumed": 0, "reservations": 0,
                "detail": "no live reservation; nothing to consume"}

    consumed_units = 0
    details = []

    for hold in holds:
        try:
            item_result = await db.execute(
                select(InventoryItem)
                .where(InventoryItem.product_id == hold.product_id)
                .with_for_update()
            )
        except Exception as exc:
            if not is_lock_contention(exc):
                raise
            await db.rollback()
            logger.warning("consume: row contended for product=%s", hold.product_id)
            raise HTTPException(status_code=409,
                                detail="Inventory row is contended; retry shortly")

        item = item_result.scalar_one_or_none()
        plan = plan_consume(
            StockLevel(item.quantity_available, item.quantity_reserved)
            if item else None,
            hold.quantity,
        )

        if plan.outcome is ConsumeOutcome.INVALID:
            await db.rollback()
            raise HTTPException(status_code=400, detail=plan.detail)

        if plan.consumed:
            item.quantity_reserved = plan.new_level.quantity_reserved
            consumed_units += hold.quantity

            db.add(OutboxMessage(
                aggregate_type="Inventory",
                aggregate_id=str(hold.product_id),
                type=plan.event,
                payload={
                    "id": str(item.id),
                    "order_id": hold.order_id,
                    "product_id": str(hold.product_id),
                    "quantity_consumed": hold.quantity,
                    "total_quantity_available": item.quantity_available,
                    "total_quantity_reserved": item.quantity_reserved,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            ))

        # Settled either way, for the same reason release settles: a hold
        # against a product row that no longer exists cannot be retired, and
        # leaving it 'held' would make every future call retry it forever.
        hold.status = "consumed"
        hold.released_at = datetime.now(timezone.utc)
        details.append(plan.detail)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("consume: order %s retired %s unit(s) across %s reservation(s)",
                request.order_id, consumed_units, len(holds))
    return {"order_id": request.order_id, "consumed": consumed_units,
            "reservations": len(holds), "detail": "; ".join(details)}
