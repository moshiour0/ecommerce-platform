import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import PaymentLedger, OutboxMessage, IdempotencyKey
from ..schemas import ChargeRequest, RefundRequest, RefundResponse

logger = logging.getLogger(__name__)

# Ledger row statuses. The ledger is append-only: a refund never mutates the
# original charge, it appends a reversing entry. Summing amount_cents for an
# order therefore always yields the true net position.
STATUS_SUCCESS = "SUCCESS"
STATUS_REFUNDED = "REFUNDED"
STATUS_REFUND_NOOP = "REFUND_NOOP"
SETTLED_STATUSES = (STATUS_REFUNDED, STATUS_REFUND_NOOP)

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


async def process_refund(db: AsyncSession, request: RefundRequest, idempotency_key: str) -> RefundResponse:
    """
    Compensating transaction for ChargePaymentCommand (Rule 5).

    Blind compensation contract: a refund for an order that was never charged
    must succeed as a no-op, never error. The saga cannot know whether a charge
    landed before a partition dropped its acknowledgement, so it compensates
    both legs unconditionally on timeout. Failing here would strand the saga.

    Idempotency is derived from ledger state rather than the idempotency-key
    cache. The ledger is the source of truth for whether money moved; a key
    table can be pruned, restored, or bypassed by a differently-keyed retry
    from the reaper, and none of those may cause a second refund.
    """
    order_id = str(request.order_id)

    # Serialize all refund attempts for this order. A row lock is not enough:
    # in the no-charge case there is no row to lock, and two concurrent no-ops
    # would each append a reversal. The advisory lock is transaction-scoped and
    # releases on commit or rollback, and needs no schema change.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:oid))"), {"oid": order_id}
    )

    # Already settled? Then this is a retry — report the prior outcome.
    settled = await db.execute(
        select(PaymentLedger)
        .where(PaymentLedger.order_id == request.order_id)
        .where(PaymentLedger.status.in_(SETTLED_STATUSES))
    )
    prior = settled.scalars().first()
    if prior:
        already_refunded = prior.status == STATUS_REFUNDED
        return RefundResponse(
            order_id=request.order_id,
            refunded=already_refunded,
            refunded_cents=abs(prior.amount_cents),
            status=prior.status,
            detail="Already settled — returning prior outcome"
        )

    # Find the charge being reversed.
    charge_result = await db.execute(
        select(PaymentLedger)
        .where(PaymentLedger.order_id == request.order_id)
        .where(PaymentLedger.status == STATUS_SUCCESS)
        .with_for_update()
    )
    charge = charge_result.scalar_one_or_none()

    if charge is None:
        # No money moved. Record the no-op so a later retry is cheap and
        # auditable, and still emit PaymentRefunded so the saga can reach
        # ROLLBACK_COMPLETED instead of stalling at TIMED_OUT.
        refunded_cents = 0
        ledger_row = PaymentLedger(
            id=uuid.uuid4(),
            order_id=request.order_id,
            user_id=request.user_id,
            amount_cents=0,
            currency="USD",
            payment_token="n/a",
            status=STATUS_REFUND_NOOP,
            created_at=datetime.now(timezone.utc)
        )
        detail = "No charge found for this order — recorded as no-op"
        logger.info(f"Refund no-op for order={order_id}: no successful charge exists")
    else:
        # Append a reversing entry. The original charge is never mutated.
        refunded_cents = charge.amount_cents
        ledger_row = PaymentLedger(
            id=uuid.uuid4(),
            order_id=request.order_id,
            user_id=charge.user_id,
            amount_cents=-charge.amount_cents,
            currency=charge.currency,
            payment_token=charge.payment_token,
            status=STATUS_REFUNDED,
            created_at=datetime.now(timezone.utc)
        )
        detail = f"Refunded {refunded_cents} cents"
        logger.info(f"Refunding order={order_id} amount_cents={refunded_cents}")

    db.add(ledger_row)

    # Rule 3: the event goes to the outbox in this same transaction, never
    # straight to Kafka. Rule: correlation — order_id is echoed back so the
    # dispatcher can map the result to its saga.
    db.add(OutboxMessage(
        aggregate_type="Payment",
        aggregate_id=order_id,
        type="PaymentRefunded",
        payload={
            "id": str(ledger_row.id),
            "order_id": order_id,
            "user_id": str(ledger_row.user_id),
            "refunded_cents": refunded_cents,
            "currency": ledger_row.currency,
            "status": ledger_row.status,
            "reason": request.reason,
            "occurred_at": ledger_row.created_at.isoformat()
        }
    ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return RefundResponse(
        order_id=request.order_id,
        refunded=charge is not None,
        refunded_cents=refunded_cents,
        status=ledger_row.status,
        detail=detail
    )
