import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import OrderSagaState, OutboxMessage, IdempotencyKey
from ..schemas import CreateOrderRequest, SagaEventRequest
from .transitions import Outcome, resolve

logger = logging.getLogger(__name__)


def _command_payload(command: str, saga_state) -> dict:
    """Payloads stay here because they read the saga row; the decision does not.

    Every command carries order_id: without it the dispatcher cannot map a
    downstream result back to a saga, and correlation degrades to caller memory.
    """
    base = {"order_id": str(saga_state.id)}
    if command == "ChargePaymentCommand":
        return {**base,
                "user_id": str(saga_state.user_id),
                "amount_cents": saga_state.total_cents}
    if command == "OrderFailed":
        return {**base,
                "user_id": str(saga_state.user_id),
                "reason": "InventoryReservationFailed"}
    return base

# KNOWN_EVENT_TYPES and TERMINAL_STATES used to be defined here as well as in
# transitions.py. Two copies of the same vocabulary is how a state machine ends
# up disagreeing with itself, so they now live in exactly one place and this
# module reads them through resolve().


async def _check_idempotency_or_return_cached(db: AsyncSession, idempotency_key: str, model_class):
    """
    Idempotency Response Contract (Rule 4):
    On conflict, return the previously committed result — never an error.
    The idempotency guard is a cache, not a gate.
    """
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
                select(model_class).where(model_class.id == idem_record.result_id)
            )
            cached = cached_result.scalar_one_or_none()
            if cached:
                return cached  # Return the previously committed result
        # Fallback: key exists but no cached result (shouldn't happen, but safe)
        raise HTTPException(status_code=409, detail="Idempotency key already processed but result not found")
    return None  # No conflict — proceed with business logic


async def start_saga(db: AsyncSession, request: CreateOrderRequest, idempotency_key: str) -> OrderSagaState:
    # Check idempotency — return cached result on retry
    cached = await _check_idempotency_or_return_cached(db, idempotency_key, OrderSagaState)
    if cached:
        return cached

    # Create Order Saga State
    saga_id = uuid.uuid4()
    saga_state = OrderSagaState(
        id=saga_id,
        user_id=request.user_id,
        status="PENDING",
        total_cents=request.total_cents,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    db.add(saga_state)

    # Emit ReserveInventoryCommand via Outbox
    outbox_message = OutboxMessage(
        aggregate_type="OrderSaga",
        aggregate_id=str(saga_id),
        type="ReserveInventoryCommand",
        payload={
            "order_id": str(saga_id),
            "user_id": str(saga_state.user_id),
            "items_payload": request.items_payload
        }
    )
    db.add(outbox_message)

    # Store result_id in idempotency key for future cache hits
    idem_result = await db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
    )
    idem_record = idem_result.scalar_one()
    idem_record.result_id = saga_id

    try:
        await db.commit()
        await db.refresh(saga_state)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return saga_state

async def advance_saga(db: AsyncSession, order_id: str, request: SagaEventRequest, idempotency_key: str) -> OrderSagaState:
    # Check idempotency — return cached result on retry
    cached = await _check_idempotency_or_return_cached(db, idempotency_key, OrderSagaState)
    if cached:
        return cached

    # Query Saga State with pessimistic locking
    result = await db.execute(
        select(OrderSagaState)
        .where(OrderSagaState.id == order_id)
        .with_for_update()
    )
    saga_state = result.scalar_one_or_none()

    if not saga_state:
        raise HTTPException(status_code=404, detail="Order Saga not found")

    # The transition table lives in transitions.py as pure data so it can be
    # tested exhaustively without a database. This function keeps persistence,
    # locking and payload construction; it no longer decides.
    #
    # Capture the status as a plain string BEFORE any rollback. db.rollback()
    # expires every object in the session, so reading saga_state.status
    # afterwards triggers a lazy reload outside the async context and raises
    # MissingGreenlet -- the endpoint then answered 500 on both the
    # out-of-order and late-duplicate paths. 500 tells a consumer to retry,
    # which is the exact opposite of what a late duplicate needs, and it would
    # have retried a terminal saga forever.
    current_status = str(saga_state.status)
    decision = resolve(current_status, request.event_type)

    if decision.outcome is not Outcome.APPLY:
        # Nothing may be committed on a non-transition. Roll back so the
        # idempotency key insert is undone and a retry is still possible; the
        # previous version logged and then committed, which burned the key and
        # destroyed every out-of-order delivery permanently.
        await db.rollback()

        if decision.outcome is Outcome.UNKNOWN_EVENT:
            # Never valid at any state — do not make the consumer retry forever.
            logger.error(
                f"Unknown saga event type={request.event_type} for order={order_id}. "
                f"Non-retryable; route to DLQ."
            )
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event type '{request.event_type}' — not retryable"
            )

        if decision.outcome is Outcome.LATE_DUPLICATE:
            # Already applied and terminal. Ack with 200 so the consumer
            # commits its offset instead of redelivering forever.
            #
            # The instance is expired by the rollback above, so it must be
            # reloaded inside async context before FastAPI serializes it.
            logger.info(
                f"Late duplicate event={request.event_type} at terminal "
                f"state={current_status} for order={order_id}. Acking as no-op."
            )
            await db.refresh(saga_state)
            return saga_state

        # Valid event, not applicable yet — almost always out-of-order delivery
        # (PaymentCharged before InventoryReserved was applied). 409 tells the
        # consumer to redeliver rather than commit the offset.
        logger.warning(
            f"Out-of-order saga event={request.event_type} at state={current_status} "
            f"for order={order_id}. Retryable — offset must not be committed."
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Event '{request.event_type}' not applicable at state "
                f"'{current_status}' — retry after the saga advances"
            )
        )

    saga_state.status = decision.new_status

    outbox_message = None
    if decision.command:
        outbox_message = OutboxMessage(
            aggregate_type="OrderSaga",
            aggregate_id=str(saga_state.id),
            type=decision.command,
            payload=_command_payload(decision.command, saga_state),
        )

    if outbox_message:
        db.add(outbox_message)

    # Store result_id in idempotency key
    idem_result = await db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == idempotency_key)
    )
    idem_record = idem_result.scalar_one()
    idem_record.result_id = saga_state.id

    try:
        await db.commit()
        await db.refresh(saga_state)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return saga_state
