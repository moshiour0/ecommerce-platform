import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import PaymentLedger, OutboxMessage, IdempotencyKey
from ..schemas import ChargeRequest

logger = logging.getLogger(__name__)

async def process_charge(db: AsyncSession, request: ChargeRequest, idempotency_key: str) -> PaymentLedger:
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
                select(PaymentLedger).where(PaymentLedger.id == idem_record.result_id)
            )
            cached = cached_result.scalar_one_or_none()
            if cached:
                return cached
        raise HTTPException(status_code=409, detail="Idempotency key already processed but result not found")

    # PSP Simulation: PENDING -> SUCCESS or FAILED
    status = "SUCCESS"
    if request.payment_token == "tok_fail":
        status = "FAILED"

    # Save Ledger entry
    payment_id = uuid.uuid4()
    ledger_entry = PaymentLedger(
        id=payment_id,
        order_id=request.order_id,
        user_id=request.user_id,
        amount_cents=request.amount_cents,
        currency=request.currency,
        payment_token=request.payment_token,
        status=status,
        created_at=datetime.now(timezone.utc)
    )
    db.add(ledger_entry)

    # Create OutboxMessage
    event_type = "PaymentCharged" if status == "SUCCESS" else "PaymentFailed"
    
    payload = {
        "id": str(ledger_entry.id),
        "order_id": str(ledger_entry.order_id),
        "user_id": str(ledger_entry.user_id),
        "amount_cents": ledger_entry.amount_cents,
        "currency": ledger_entry.currency,
        "status": ledger_entry.status,
        "created_at": ledger_entry.created_at.isoformat()
    }
    
    outbox_message = OutboxMessage(
        aggregate_type="Payment",
        aggregate_id=str(ledger_entry.order_id),
        type=event_type,
        payload=payload
    )
    db.add(outbox_message)

    # Store result_id in idempotency key
    idem_result = await db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
    )
    idem_record = idem_result.scalar_one()
    idem_record.result_id = payment_id

    try:
        await db.commit()
        await db.refresh(ledger_entry)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return ledger_entry
