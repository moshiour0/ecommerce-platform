import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import NotificationRecord, OutboxMessage, IdempotencyKey
from ..schemas import NotificationRequest

async def schedule_notification(db: AsyncSession, request: NotificationRequest, idempotency_key: str) -> NotificationRecord:
    # Check idempotency
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")

    # Save Notification Record
    notification_id = uuid.uuid4()
    notification = NotificationRecord(
        id=notification_id,
        user_id=request.user_id,
        channel=request.channel,
        template_name=request.template_name,
        status="PENDING",
        payload=request.payload,
        created_at=datetime.now(timezone.utc)
    )
    db.add(notification)

    # Create OutboxMessage for async dispatch
    outbox_payload = {
        "id": str(notification.id),
        "user_id": str(notification.user_id),
        "channel": notification.channel,
        "template_name": notification.template_name,
        "status": notification.status,
        "payload": notification.payload,
        "created_at": notification.created_at.isoformat()
    }
    
    outbox_message = OutboxMessage(
        aggregate_type="Notification",
        aggregate_id=str(notification.id),
        type="NotificationDispatchRequested",
        payload=outbox_payload
    )
    db.add(outbox_message)

    try:
        await db.commit()
        await db.refresh(notification)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return notification
