import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import OrderSagaState, OutboxMessage, IdempotencyKey
from ..schemas import CreateOrderRequest, SagaEventRequest

logger = logging.getLogger(__name__)

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
    elif request.event_type == "OrderCompleted" and saga_state.status == "PAID":
        saga_state.status = "ORDER_COMPLETED"
    elif request.event_type == "InventoryReleased" and saga_state.status == "FAILED":
        saga_state.status = "ROLLBACK_COMPLETED"
    else:
        # S-3 Fix: Log invalid transitions instead of silently swallowing
        logger.warning(
            f"Unexpected saga transition: event={request.event_type} "
            f"at state={saga_state.status} for order={order_id}. Ignoring."
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
