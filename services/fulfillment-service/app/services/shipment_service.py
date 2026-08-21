"""
Shipments, courier callbacks, and settlement reconciliation.

The decisions live in courier_rules and are unit tested. What lives here is the
transaction and the one outbound call: a mapped status that drives a seller
order has to reach order-saga, which owns the COD lifecycle.

Why this calls order-saga synchronously
---------------------------------------
The trigger is external. A courier pushes a status and expects an answer, and
it retries on a failure — which is exactly the redelivery an outbox would have
provided, already supplied by the caller. Recording the callback and hoping a
worker picks it up later would add a hop and a second failure mode to a path
that already has retry built in.

Behind a Rule 11 breaker, and the shipment records what happened either way:
if order-saga is unreachable the status is still stored, so the parcel's
history is complete and the callback can be replayed by hand.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from python_common.resilience import (
    AsyncCircuitBreaker, BulkheadFullError, CircuitOpenError,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models import OutboxMessage, SettlementRow, Shipment
from . import courier_registry
from .courier_rules import (
    ReconcileOutcome, action_for, reconcile_row, summarise_settlement,
)

logger = logging.getLogger(__name__)

ORDER_SAGA_URL = os.getenv("ORDER_SAGA_URL", "http://order-saga:8012")
SAGA_TIMEOUT = float(os.getenv("ORDER_SAGA_TIMEOUT", "5.0"))

_breaker = AsyncCircuitBreaker("order-saga", bulkhead=10)


async def _drive_seller_order(order_id, seller_order_id, action: str,
                              reason: str) -> str:
    """Ask order-saga to move the seller order. Returns a short outcome word.

    Never raises. A courier callback that 500s because a downstream is busy
    gets retried by the courier as an unrecognised failure, and the parcel's
    status would be lost in the meantime; recording the status and reporting
    what happened to the transition is more useful than failing the whole
    callback.
    """
    async def _post():
        async with httpx.AsyncClient(timeout=SAGA_TIMEOUT) as client:
            return await client.post(
                f"{ORDER_SAGA_URL}/orders/{order_id}/seller-orders/"
                f"{seller_order_id}/{action}",
                json={"reason": reason} if reason else {})

    try:
        response = await _breaker.call(_post)
    except (CircuitOpenError, BulkheadFullError) as e:
        logger.warning("order-saga unavailable for %s: %s", action, e)
        return "unavailable"
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("order-saga unreachable for %s: %s", action, e)
        return "unreachable"

    if response.status_code == 200:
        return "applied"
    if response.status_code == 409:
        # The seller order is not in a state this action can leave. Couriers
        # resend callbacks, and a repeated `delivered` on an already delivered
        # parcel lands here -- benign, and not worth alarming about.
        logger.info("order-saga refused %s: %s", action,
                    response.text[:200])
        return "not_applicable"
    logger.error("order-saga rejected %s: %s %s", action,
                 response.status_code, response.text[:200])
    return f"rejected_{response.status_code}"


async def create_shipment(db: AsyncSession, request) -> Shipment:
    """Hand a seller order to a courier.

    The provider must be one the registry knows. An unregistered courier is
    refused rather than accepted with no mapping: its callbacks would arrive,
    map to nothing, and the parcel would sit at PICKUP_PENDING while somebody
    physically delivered it.
    """
    courier = courier_registry.get(request.provider)
    if courier is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown courier {request.provider!r}; configured: "
                   f"{', '.join(courier_registry.known())}. A courier with no "
                   f"status mapping would accept callbacks and move nothing.")

    existing = await db.execute(
        select(Shipment).where(Shipment.seller_order_id == request.seller_order_id))
    already = existing.scalar_one_or_none()
    if already is not None:
        # One parcel per seller order. A second would mean two couriers both
        # collecting cash for it.
        raise HTTPException(
            status_code=409,
            detail=f"seller order {request.seller_order_id} already has a "
                   f"shipment with {already.provider}")

    shipment = Shipment(
        seller_order_id=request.seller_order_id,
        order_id=request.order_id,
        provider=courier.provider,
        tracking_code=request.tracking_code,
        status="PICKUP_PENDING",
        cod_amount_cents=request.cod_amount_cents,
        currency=request.currency.upper(),
    )
    db.add(shipment)
    await db.flush()

    db.add(OutboxMessage(
        aggregate_type="Shipment",
        aggregate_id=str(shipment.id),
        type="ShipmentCreated",
        payload={
            "shipment_id": str(shipment.id),
            "seller_order_id": str(shipment.seller_order_id),
            "order_id": str(shipment.order_id),
            "provider": shipment.provider,
            "tracking_code": shipment.tracking_code,
            "cod_amount_cents": shipment.cod_amount_cents,
        },
    ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("shipment %s created with %s", shipment.id, shipment.provider)
    return shipment


async def handle_callback(db: AsyncSession, provider: str, request) -> dict:
    """A courier says a parcel moved.

    Three outcomes, and they are deliberately different:

      * a mapped status that drives an action -- order-saga is asked to move
        the seller order;
      * a mapped status that drives nothing -- recorded, and that is all
        (IN_TRANSIT is not a decision);
      * an unmapped word -- recorded verbatim, flagged, and parked for a
        human. Never guessed: defaulting to IN_TRANSIT hides a delivery and
        defaulting to DELIVERED consumes stock on a word nobody has read.
    """
    courier = courier_registry.get(provider)
    if courier is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown courier {provider!r}")

    query = select(Shipment).where(Shipment.provider == courier.provider)
    if request.tracking_code:
        query = query.where(Shipment.tracking_code == request.tracking_code)
    elif request.seller_order_id:
        query = query.where(Shipment.seller_order_id == request.seller_order_id)
    else:
        raise HTTPException(
            status_code=422,
            detail="a callback must identify the parcel by tracking_code or "
                   "seller_order_id")

    result = await db.execute(query.with_for_update())
    shipment = result.scalar_one_or_none()
    if shipment is None:
        raise HTTPException(status_code=404, detail="no such shipment")

    canonical = courier.canonical(request.status)
    occurred = request.occurred_at or datetime.now(timezone.utc)

    shipment.raw_status = request.status
    shipment.raw_status_at = occurred
    shipment.unmapped = canonical is None
    if canonical is not None:
        shipment.status = canonical.value

    action = action_for(canonical)

    db.add(OutboxMessage(
        aggregate_type="Shipment",
        aggregate_id=str(shipment.id),
        type="ShipmentStatusUnmapped" if canonical is None
             else "ShipmentStatusChanged",
        payload={
            "shipment_id": str(shipment.id),
            "seller_order_id": str(shipment.seller_order_id),
            "order_id": str(shipment.order_id),
            "provider": shipment.provider,
            "raw_status": request.status,
            "canonical_status": canonical.value if canonical else None,
            "action": action,
        },
    ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    if canonical is None:
        logger.warning("courier %s sent unmapped status %r for shipment %s",
                       provider, request.status, shipment.id)

    action_result = None
    if action:
        action_result = await _drive_seller_order(
            shipment.order_id, shipment.seller_order_id, action,
            f"courier {provider} reported {request.status}")

    return {
        "shipment_id": shipment.id,
        "provider": shipment.provider,
        "raw_status": request.status,
        "canonical_status": canonical.value if canonical else None,
        "action": action,
        "action_result": action_result,
        "unmapped": canonical is None,
    }


async def _seller_order_snapshot(order_id, seller_order_id):
    """What order-saga believes about this seller order, for reconciliation."""
    try:
        async with httpx.AsyncClient(timeout=SAGA_TIMEOUT) as client:
            response = await client.get(
                f"{ORDER_SAGA_URL}/orders/{order_id}/seller-orders")
    except (httpx.TimeoutException, httpx.RequestError):
        return None
    if response.status_code != 200:
        return None
    for seller_order in response.json().get("seller_orders", []):
        if seller_order["id"] == str(seller_order_id):
            return {"expected_cents": seller_order["subtotal_cents"],
                    "status": seller_order["status"]}
    return None


async def ingest_settlement(db: AsyncSession, request) -> dict:
    """Reconcile a courier's remittance file against what was expected.

    Nothing here moves money. It classifies every row and stores the result,
    including the rows that did not reconcile — a rejected row that is
    forgotten is a dispute nobody can reconstruct. §3d: a mismatch is an
    operational alert, never a silent adjustment.
    """
    courier = courier_registry.get(request.provider)
    if courier is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown courier {request.provider!r}")

    results = []
    for row in request.rows:
        reference = row.reference.strip()

        # The reference is the shipment's tracking code, which is what a
        # courier knows about; the platform's own ids never leave it.
        shipment_result = await db.execute(
            select(Shipment)
            .where(Shipment.provider == courier.provider)
            .where(Shipment.tracking_code == reference))
        shipment = shipment_result.scalar_one_or_none()

        known = None
        seller_order_id = None
        if shipment is not None:
            seller_order_id = shipment.seller_order_id
            known = await _seller_order_snapshot(shipment.order_id,
                                                 shipment.seller_order_id)
            if known is None:
                # order-saga could not answer. The expectation copied onto the
                # shipment at dispatch is the fallback, and it is the figure
                # that was actually agreed when the parcel went out.
                known = {"expected_cents": shipment.cod_amount_cents,
                         "status": "DELIVERED"
                         if shipment.status == "DELIVERED" else shipment.status}

        settled_before = False
        if seller_order_id is not None:
            prior = await db.execute(
                select(SettlementRow)
                .where(SettlementRow.seller_order_id == seller_order_id)
                .where(SettlementRow.outcome == ReconcileOutcome.MATCHED.value))
            settled_before = prior.scalars().first() is not None

        outcome = reconcile_row(
            {"reference": reference, "collected_cents": row.collected_cents},
            known, already_settled=settled_before)

        db.add(SettlementRow(
            provider=courier.provider,
            batch_reference=request.batch_reference,
            row_reference=reference,
            seller_order_id=seller_order_id,
            expected_cents=outcome.expected_cents,
            collected_cents=outcome.remitted_cents,
            outcome=outcome.outcome.value,
            detail=outcome.detail[:1024],
        ))
        results.append(outcome)

    summary = summarise_settlement(results)

    db.add(OutboxMessage(
        aggregate_type="Settlement",
        aggregate_id=f"{request.provider}:{request.batch_reference}",
        type="SettlementReconciled",
        payload={
            "provider": request.provider,
            "batch_reference": request.batch_reference,
            **summary,
        },
    ))

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        # A resent batch collides on (provider, batch_reference, row_reference).
        # That is the uniqueness that makes a resend a no-op rather than a
        # second payout, so it is a 409 and not a 500.
        raise HTTPException(
            status_code=409,
            detail=f"batch {request.batch_reference} has already been "
                   f"ingested for {request.provider}") from e

    if summary["needs_attention"]:
        logger.warning("settlement %s/%s: %s row(s) need attention, variance %s",
                       request.provider, request.batch_reference,
                       summary["needs_attention"], summary["variance_cents"])

    return {"provider": request.provider,
            "batch_reference": request.batch_reference, **summary}
