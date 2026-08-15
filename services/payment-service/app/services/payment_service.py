import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import PaymentLedger, OutboxMessage, IdempotencyKey
from ..schemas import ChargeRequest, RefundRequest, RefundResponse
from .refund_rules import LedgerEntry, plan_refund
from .charge_rules import (
    IdempotencyOutcome, authorize, event_for, resolve_idempotency,
)

logger = logging.getLogger(__name__)

# Ledger row statuses. The ledger is append-only: a refund never mutates the
# original charge, it appends a reversing entry. Summing amount_cents for an
# order therefore always yields the true net position.
# Ledger statuses live in refund_rules, which is the single definition the
# unit tests exercise; duplicating them here is how two copies of one
# vocabulary start disagreeing.
from .refund_rules import (  # noqa: E402
    STATUS_SUCCESS, STATUS_REFUNDED, STATUS_REFUND_NOOP, SETTLED_STATUSES,
)

async def process_charge(db: AsyncSession, request: ChargeRequest, idempotency_key: str) -> PaymentLedger:
    # Check idempotency — return cached result on retry (Rule 4)
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    key_was_inserted = result.rowcount == 1

    stored_result_id, cached = None, None
    if not key_was_inserted:
        existing_key = await db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
        )
        idem_record = existing_key.scalar_one_or_none()
        stored_result_id = idem_record.result_id if idem_record else None
        if stored_result_id is not None:
            cached_result = await db.execute(
                select(PaymentLedger).where(PaymentLedger.id == stored_result_id)
            )
            cached = cached_result.scalar_one_or_none()

    decision = resolve_idempotency(
        key_was_inserted=key_was_inserted,
        stored_result_id=stored_result_id,
        cached_result_found=cached is not None,
    )

    if decision.outcome is IdempotencyOutcome.RETURN_CACHED:
        # Rule 4: the guard is a cache, not a gate. Replay, never error.
        return cached

    if decision.outcome is IdempotencyOutcome.RETRY_LATER:
        # No committed result to replay: either the same key is still in
        # flight, or its result row is gone. Proceeding would authorize the
        # card a second time, so answer retryably instead. 409 is what the
        # dispatcher already treats as "try again".
        logger.warning(
            f"Idempotent charge cannot complete yet: {decision.detail} "
            f"(key={idempotency_key})"
        )
        raise HTTPException(status_code=409, detail=decision.detail)

    # PSP outcome and the event it produces both live in charge_rules, where
    # they are tested against order-saga's vocabulary.
    status = authorize(request.payment_token)

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
    event_type = event_for(status)
    
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

    # Already settled? Then this is a retry.
    settled = await db.execute(
        select(PaymentLedger)
        .where(PaymentLedger.order_id == request.order_id)
        .where(PaymentLedger.status.in_(SETTLED_STATUSES))
    )
    prior = settled.scalars().first()

    # Find the charge being reversed.
    charge_result = await db.execute(
        select(PaymentLedger)
        .where(PaymentLedger.order_id == request.order_id)
        .where(PaymentLedger.status == STATUS_SUCCESS)
        .with_for_update()
    )
    charge = charge_result.scalar_one_or_none()

    # The decision lives in refund_rules so the money path is testable without
    # a database. This function keeps locking, querying and persistence.
    plan = plan_refund(
        prior=LedgerEntry(prior.status, prior.amount_cents) if prior else None,
        charge=LedgerEntry(charge.status, charge.amount_cents) if charge else None,
    )

    if plan.append_status is None:
        # Already settled: append nothing, emit nothing, report the prior
        # outcome. Appending here is exactly what a second refund looks like.
        return RefundResponse(
            order_id=request.order_id,
            refunded=plan.refunded,
            refunded_cents=plan.refunded_cents,
            status=prior.status,
            detail=plan.detail,
        )

    # Identity fields come from the charge when one exists, so the reversal
    # carries the same currency and token as the entry it reverses.
    ledger_row = PaymentLedger(
        id=uuid.uuid4(),
        order_id=request.order_id,
        user_id=charge.user_id if charge else request.user_id,
        amount_cents=plan.append_amount_cents,
        currency=charge.currency if charge else "USD",
        payment_token=charge.payment_token if charge else "n/a",
        status=plan.append_status,
        created_at=datetime.now(timezone.utc),
    )
    refunded_cents = plan.refunded_cents
    detail = plan.detail
    logger.info(f"Refund plan for order={order_id}: {plan.outcome.value} "
                f"amount_cents={plan.append_amount_cents}")

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
        refunded=plan.refunded,
        refunded_cents=plan.refunded_cents,
        status=ledger_row.status,
        detail=detail,
    )
