import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import OrderSagaState, OutboxMessage, IdempotencyKey
from ..schemas import CreateOrderRequest, SagaEventRequest

logger = logging.getLogger(__name__)

# Event types this state machine understands. An event outside this set can
# never become valid, so it is non-retryable and belongs in the DLQ.
KNOWN_EVENT_TYPES = {
    "InventoryReserved",
    "InventoryReservationFailed",
    "PaymentCharged",
    "PaymentFailed",
    "PaymentRefunded",
    "OrderCompleted",
    "InventoryReleased",
}

# States from which no further transition is possible. A known event arriving
# here is a late duplicate, not an error — ack it so the consumer can commit.
TERMINAL_STATES = {"ORDER_COMPLETED", "ROLLBACK_COMPLETED"}

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

    outbox_message = None

    # Handle transitions
    if request.event_type == "InventoryReserved" and saga_state.status == "PENDING":
        saga_state.status = "INVENTORY_RESERVED"
        outbox_message = OutboxMessage(
            aggregate_type="OrderSaga",
            aggregate_id=str(saga_state.id),
            type="ChargePaymentCommand",
            payload={
                "order_id": str(saga_state.id),
                "user_id": str(saga_state.user_id),
                "amount_cents": saga_state.total_cents
            }
        )
    elif request.event_type == "PaymentCharged" and saga_state.status == "INVENTORY_RESERVED":
        saga_state.status = "PAID"
        outbox_message = OutboxMessage(
            aggregate_type="OrderSaga",
            aggregate_id=str(saga_state.id),
            type="ConfirmOrderCommand",
            payload={
                "order_id": str(saga_state.id)
            }
        )
    elif request.event_type == "PaymentFailed" and saga_state.status == "INVENTORY_RESERVED":
        saga_state.status = "FAILED"
        outbox_message = OutboxMessage(
            aggregate_type="OrderSaga",
            aggregate_id=str(saga_state.id),
            type="ReleaseInventoryCommand",
            payload={
                "order_id": str(saga_state.id)
            }
        )
    elif request.event_type == "InventoryReservationFailed" and saga_state.status == "PENDING":
        # Out-of-stock is a business outcome, not an exception. Before this arm
        # existed, inventory raised HTTP 400 and emitted nothing, so every
        # oversubscribed saga in a flash sale hung at PENDING until the reaper
        # swept it 15 minutes later. Nothing was reserved, so there is nothing
        # to compensate — this is terminal.
        saga_state.status = "ROLLBACK_COMPLETED"
        outbox_message = OutboxMessage(
            aggregate_type="OrderSaga",
            aggregate_id=str(saga_state.id),
            type="OrderFailed",
            payload={
                "order_id": str(saga_state.id),
                "user_id": str(saga_state.user_id),
                "reason": "InventoryReservationFailed"
            }
        )
    elif request.event_type == "PaymentRefunded" and saga_state.status in ("PAID", "TIMED_OUT"):
        # Completes the compensation loop opened by RefundPaymentCommand.
        saga_state.status = "ROLLBACK_COMPLETED"
    elif request.event_type == "OrderCompleted" and saga_state.status == "PAID":
        saga_state.status = "ORDER_COMPLETED"
    elif request.event_type == "InventoryReleased" and saga_state.status in ("FAILED", "TIMED_OUT"):
        # TIMED_OUT is reached via the reaper, which emits ReleaseInventoryCommand.
        # Without this arm the resulting ack had no transition and the saga could
        # never reach ROLLBACK_COMPLETED, making "released" and "leaked" identical.
        saga_state.status = "ROLLBACK_COMPLETED"
    else:
        # S-3 (real fix): the previous version logged, then fell through to
        # commit — burning the idempotency key and acking the event. Any
        # out-of-order delivery was destroyed permanently, because redelivery
        # then hit the idempotency cache and returned stale state.
        #
        # Nothing may be committed on a non-transition. Roll back so the
        # idempotency key insert is undone and a retry is still possible.
        await db.rollback()

        if request.event_type not in KNOWN_EVENT_TYPES:
            # Never valid at any state — do not make the consumer retry forever.
            # Non-2xx and non-retryable: the consumer routes this to dlq.<topic>.
            logger.error(
                f"Unknown saga event type={request.event_type} for order={order_id}. "
                f"Non-retryable; route to DLQ."
            )
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event type '{request.event_type}' — not retryable"
            )

        if saga_state.status in TERMINAL_STATES:
            # Late duplicate of an event we already applied. Safe no-op: return
            # current state with 200 so the consumer commits its offset.
            logger.info(
                f"Late duplicate event={request.event_type} at terminal "
                f"state={saga_state.status} for order={order_id}. Acking as no-op."
            )
            return saga_state

        # Known event, non-terminal state, but not valid *yet* — almost always
        # out-of-order delivery (e.g. PaymentCharged before InventoryReserved
        # was applied). Retryable: 409 tells the consumer to redeliver rather
        # than commit the offset.
        logger.warning(
            f"Out-of-order saga event={request.event_type} at state={saga_state.status} "
            f"for order={order_id}. Retryable — offset must not be committed."
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Event '{request.event_type}' not applicable at state "
                f"'{saga_state.status}' — retry after the saga advances"
            )
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
