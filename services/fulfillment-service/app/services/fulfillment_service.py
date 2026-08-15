import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import FulfillmentRecord, OutboxMessage, IdempotencyKey
from ..schemas import DispatchRequest

async def dispatch_order(db: AsyncSession, request: DispatchRequest, idempotency_key: str) -> FulfillmentRecord:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    tracking_number = f"TRK-{uuid.uuid4().hex[:12].upper()}"
    courier = "SwissPost"

    # GEOSPATIAL LOGISTICS ROUTING (Blatten hazard check via SAR telemetry mockup)
    if request.destination_region == "Blatten":
        status = "HELD_FOR_CLEARANCE"
        event_type = "OrderHeld"
    else:
        status = "DISPATCHED"
        event_type = "OrderDispatched"

    # Save Fulfillment entry
    fulfillment_id = uuid.uuid4()
    fulfillment = FulfillmentRecord(
        id=fulfillment_id,
        order_id=request.order_id,
        destination_region=request.destination_region,
        tracking_number=tracking_number,
        courier=courier,
        status=status,
        created_at=datetime.now(timezone.utc)
    )
    db.add(fulfillment)

    # Create OutboxMessage
    payload = {
        "id": str(fulfillment.id),
        "order_id": str(fulfillment.order_id),
        "destination_region": fulfillment.destination_region,
        "tracking_number": fulfillment.tracking_number,
        "courier": fulfillment.courier,
        "status": fulfillment.status,
        "created_at": fulfillment.created_at.isoformat()
    }
    
    outbox_message = OutboxMessage(
        aggregate_type="Fulfillment",
        aggregate_id=str(fulfillment.order_id),
        type=event_type,
        payload=payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(fulfillment)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return fulfillment
