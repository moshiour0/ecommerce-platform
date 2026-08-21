import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from ..models import OrderLine, SellerOrder, OrderSagaState, OutboxMessage, IdempotencyKey
from ..schemas import CreateOrderRequest, SagaEventRequest
from .transitions import Outcome, resolve
from .cod_rules import InventoryEffect, plan_transition
from .split_rules import (
    EVENT_SELLER_ORDER_CREATED, SplitError, build_seller_order_event,
    derive_order_status, goods_subtotal_cents, is_order_complete,
    split_by_seller,
)

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

    # Split into one order per seller, before anything else is written.
    #
    # Refused as a whole if any line cannot be attributed to a seller: a line
    # nobody can be paid for is a line nobody can be asked to ship, and
    # creating the seller orders that *do* parse would leave that line in an
    # order that can never complete -- with stock reserved against it, because
    # the reservation command below covers every line regardless.
    try:
        plans = split_by_seller(request.items_payload)
    except SplitError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    for plan in plans:
        seller_order_id = uuid.uuid4()
        db.add(SellerOrder(
            id=seller_order_id,
            order_id=saga_id,
            seller_id=uuid.UUID(plan.seller_id),
            status="PENDING",
            subtotal_cents=plan.subtotal_cents,
            item_count=plan.item_count,
        ))
        for line in plan.lines:
            db.add(OrderLine(
                order_id=saga_id,
                seller_order_id=seller_order_id,
                product_id=uuid.UUID(line["product_id"]),
                seller_id=uuid.UUID(plan.seller_id),
                quantity=line["quantity"],
                price_cents=line["price_cents"],
                line_total_cents=line["line_total_cents"],
            ))

        # One event per seller order. The seller has to hear about their own
        # order, and a consumer acting on it -- a notification, a courier
        # assignment, the seller dashboard -- should not have to ask order-saga
        # what is in it.
        db.add(OutboxMessage(
            aggregate_type="SellerOrder",
            aggregate_id=str(seller_order_id),
            type=EVENT_SELLER_ORDER_CREATED,
            payload=build_seller_order_event(
                str(saga_id), str(seller_order_id), str(saga_state.user_id),
                plan, "PENDING"),
        ))

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
    decision = resolve(current_status, request.event_type,
                       getattr(saga_state, 'payment_method', 'CARD'))

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

    # Carry the reservation down to the seller orders.
    #
    # Keyed on the event rather than on the parent's new status: under COD the
    # parent goes straight from PENDING to ORDER_COMPLETED and never passes
    # through INVENTORY_RESERVED, so keying on the status would leave every
    # COD seller order stuck at PENDING -- which is exactly what the first run
    # of this did.
    #
    # The reservation is order-wide today: one ReserveInventoryCommand covers
    # every line whoever sells it, so every seller order becomes reserved at
    # the same moment.
    #
    # Only PENDING children are moved. A seller order that has already been
    # cancelled must not be resurrected by a late InventoryReserved.
    if request.event_type == "InventoryReserved":
        await db.execute(
            SellerOrder.__table__.update()
            .where(SellerOrder.order_id == saga_state.id)
            .where(SellerOrder.status == "PENDING")
            .values(status="INVENTORY_RESERVED",
                    updated_at=datetime.now(timezone.utc))
        )

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


async def get_seller_orders(db: AsyncSession, order_id):
    """This order, broken down by who has to ship it.

    The buyer-facing status here is *derived* from the children rather than
    read from order_saga_states.status, and the two can legitimately differ
    while the COD lifecycle is unimplemented: the saga still drives the parent
    through its own vocabulary (PENDING -> INVENTORY_RESERVED -> PAID -> ...)
    while every seller order sits at PENDING. Both are reported, labelled, so
    the difference is visible rather than reconciled by whichever one a caller
    happens to read.
    """
    result = await db.execute(
        select(SellerOrder).where(SellerOrder.order_id == order_id)
        .order_by(SellerOrder.seller_id))
    seller_orders = result.scalars().all()

    lines_result = await db.execute(
        select(OrderLine).where(OrderLine.order_id == order_id)
        .order_by(OrderLine.product_id))
    lines = lines_result.scalars().all()

    by_seller_order = {}
    for line in lines:
        by_seller_order.setdefault(line.seller_order_id, []).append({
            "product_id": str(line.product_id),
            "quantity": line.quantity,
            "price_cents": line.price_cents,
            "line_total_cents": line.line_total_cents,
        })

    statuses = [so.status for so in seller_orders]
    return {
        "order_id": str(order_id),
        "seller_order_count": len(seller_orders),
        "derived_status": derive_order_status(statuses),
        "complete": is_order_complete(statuses),
        # Goods across every seller order. Deliberately not the order total,
        # which also carries tax, shipping and promotions.
        "goods_subtotal_cents": sum(so.subtotal_cents for so in seller_orders),
        "seller_orders": [
            {
                "id": str(so.id),
                "seller_id": str(so.seller_id),
                "status": so.status,
                "subtotal_cents": so.subtotal_cents,
                "currency": so.currency,
                "item_count": so.item_count,
                "lines": by_seller_order.get(so.id, []),
            }
            for so in seller_orders
        ],
    }


# The inventory command each effect turns into. Emitted through the outbox and
# executed by saga-dispatcher, never called from here: Rule 3 keeps state and
# its consequences in one transaction, and a direct HTTP call inside a
# database transaction is how half-applied transitions happen.
_EFFECT_COMMANDS = {
    InventoryEffect.RELEASE: "ReleaseSellerOrderInventoryCommand",
    InventoryEffect.CONSUME: "ConsumeSellerOrderInventoryCommand",
}


async def transition_seller_order(db: AsyncSession, order_id, seller_order_id,
                                  action: str, request):
    """Move one seller order along the COD lifecycle.

    The status change, its event, and any inventory command all commit
    together. A delivery that consumed stock without recording the delivery --
    or recorded it without consuming -- would be a warehouse and a database
    that disagree, and nothing would notice until a stock count.
    """
    result = await db.execute(
        select(SellerOrder)
        .where(SellerOrder.id == seller_order_id)
        .where(SellerOrder.order_id == order_id)
        .with_for_update())
    seller_order = result.scalar_one_or_none()
    if seller_order is None:
        raise HTTPException(status_code=404, detail="Seller order not found")

    reason = (request.reason or "") if request else ""
    decision = plan_transition(action, seller_order.status, reason)
    if not decision.ok:
        await db.rollback()
        raise HTTPException(status_code=decision.http_status,
                            detail=decision.detail)

    now = datetime.now(timezone.utc)
    seller_order.status = decision.to.value
    seller_order.status_reason = decision.detail or None

    stamp = {"confirm": "confirmed_at", "dispatch": "dispatched_at",
             "deliver": "delivered_at", "settle": "settled_at"}.get(action)
    if stamp:
        setattr(seller_order, stamp, now)

    if action == "dispatch" and request is not None:
        seller_order.courier_name = request.courier_name
        seller_order.tracking_code = request.tracking_code

    # Which products this seller order covers. The inventory command is scoped
    # to them: one seller cancelling must not release another seller's still
    # live stock out of the same order.
    lines_result = await db.execute(
        select(OrderLine.product_id)
        .where(OrderLine.seller_order_id == seller_order.id))
    product_ids = [str(row[0]) for row in lines_result.all()]

    db.add(OutboxMessage(
        aggregate_type="SellerOrder",
        aggregate_id=str(seller_order.id),
        type=decision.event,
        payload={
            "order_id": str(order_id),
            "seller_order_id": str(seller_order.id),
            "seller_id": str(seller_order.seller_id),
            "status": seller_order.status,
            "status_reason": seller_order.status_reason,
            "subtotal_cents": seller_order.subtotal_cents,
            "courier_name": seller_order.courier_name,
            "tracking_code": seller_order.tracking_code,
            "product_ids": product_ids,
        },
    ))

    command = _EFFECT_COMMANDS.get(decision.effect)
    if command:
        db.add(OutboxMessage(
            aggregate_type="SellerOrder",
            aggregate_id=str(seller_order.id),
            type=command,
            payload={
                "order_id": str(order_id),
                "seller_order_id": str(seller_order.id),
                "product_ids": product_ids,
            },
        ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("seller order %s -> %s (%s)", seller_order.id,
                seller_order.status, decision.effect.value)
    return {
        "seller_order_id": str(seller_order.id),
        "order_id": str(order_id),
        "seller_id": str(seller_order.seller_id),
        "status": seller_order.status,
        "status_reason": seller_order.status_reason,
        "inventory_effect": decision.effect.value,
        "courier_name": seller_order.courier_name,
        "tracking_code": seller_order.tracking_code,
    }
