"""
Saga Dispatcher (Architecture section 3b).

Closes the saga command/event loop. The order-saga writes a command to its
outbox in the same transaction as the state change; this worker claims that
command, invokes the owning service, and feeds the result back as a state
transition. It translates -- it never decides. All business rules live in
order-saga's state machine.

Why this exists as a service: the previous implementation lived in
tests/e2e/saga_orchestrator_worker.py, so the state machine could not advance
outside a test run. Nothing in services/ or workers/ consumed saga commands.

Transport: this claims commands directly from the order_db outbox with
FOR UPDATE SKIP LOCKED. That is the polling-publisher variant of the outbox
pattern -- it does not violate Rule 3 (nothing publishes to Kafka directly;
Debezium still streams the same rows to OrderSaga.events for every other
consumer), but it is not the Kafka consumption described in the diagram.
Moving the claim loop onto OrderSaga.events is the next step now that all 15
CDC connectors are healthy; the invoke/feedback logic below is transport
agnostic and does not change when that happens.
"""

import asyncio
import logging
import os
import uuid

import asyncpg
import httpx
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_handler)

CONSUMER_NAME = "saga-dispatcher"

ORDER_DB_URL = os.getenv("ORDER_DATABASE_URL", "postgresql://admin:supersecret@postgres:5432/order_db")
SAGA_URL = os.getenv("SAGA_URL", "http://order-saga:8012/orders")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8013/inventory")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8015/payments")

POLL_INTERVAL = float(os.getenv("DISPATCH_POLL_SECONDS", "2"))
BATCH_SIZE = int(os.getenv("DISPATCH_BATCH_SIZE", "20"))
HTTP_TIMEOUT = float(os.getenv("DISPATCH_HTTP_TIMEOUT", "10"))

app = FastAPI(title="Saga Dispatcher")
_dispatch_task = None
_stats = {"claimed": 0, "advanced": 0, "deferred": 0, "dead_lettered": 0, "errors": 0}


async def _advance_saga(client: httpx.AsyncClient, order_id: str, event_type: str,
                        idem_key: str, payload: dict | None = None) -> bool:
    """Feed a result back into the state machine.

    Returns True when the command is settled and may be marked processed.

    The saga distinguishes three non-2xx outcomes and they must be honoured
    rather than collapsed into "error":
      409 -- known event, not applicable yet (out-of-order). Retry later;
             leave the command unprocessed so the next tick re-attempts.
      422 -- unknown event type. Never valid; do not retry forever.
      2xx -- applied, or acknowledged as a late duplicate at a terminal state.
    """
    res = await client.post(
        f"{SAGA_URL}/{order_id}/events",
        json={"event_type": event_type, "payload": payload or {}},
        headers={"Idempotency-Key": idem_key},
        timeout=HTTP_TIMEOUT,
    )
    if res.status_code < 300:
        _stats["advanced"] += 1
        logger.info(f"Saga advanced: order={order_id} event={event_type}")
        return True
    if res.status_code == 409:
        _stats["deferred"] += 1
        logger.warning(f"Out-of-order, will retry: order={order_id} event={event_type}")
        return False
    if res.status_code == 422:
        _stats["dead_lettered"] += 1
        logger.error(f"Non-retryable event, dead-lettering: order={order_id} "
                     f"event={event_type} detail={res.text[:200]}")
        return True  # settled: retrying can never succeed
    _stats["errors"] += 1
    logger.error(f"Saga rejected event: order={order_id} event={event_type} "
                 f"status={res.status_code} body={res.text[:200]}")
    return False


async def _handle(client: httpx.AsyncClient, msg_id, msg_type: str, payload: dict) -> bool:
    """Invoke the service that owns this command; return True when settled."""
    order_id = payload.get("order_id")
    if not order_id:
        logger.error(f"Command {msg_type} ({msg_id}) has no order_id; cannot correlate")
        return True  # unroutable forever — settle rather than spin

    headers = {"Idempotency-Key": str(msg_id)}

    if msg_type == "ReserveInventoryCommand":
        items = payload.get("items_payload", [])
        if isinstance(items, dict) and "items" in items:
            items = items["items"]
        if not items:
            return await _advance_saga(client, order_id, "InventoryReservationFailed",
                                       f"inv-fail-{msg_id}", {"reason": "EmptyItemsPayload"})
        item = items[0]
        res = await client.post(
            f"{INVENTORY_URL}/reserve",
            json={"product_id": item.get("product_id"), "quantity": item.get("quantity", 1)},
            headers=headers, timeout=HTTP_TIMEOUT,
        )
        if res.status_code == 200:
            return await _advance_saga(client, order_id, "InventoryReserved", f"inv-res-{msg_id}")
        # 409 insufficient stock / 404 unknown product are business outcomes,
        # not transport failures. The saga has a transition for them.
        return await _advance_saga(client, order_id, "InventoryReservationFailed",
                                   f"inv-fail-{msg_id}",
                                   {"reason": "InsufficientStock", "status_code": res.status_code})

    if msg_type == "ChargePaymentCommand":
        res = await client.post(
            f"{PAYMENT_URL}/charge",
            json={"order_id": order_id, "user_id": payload.get("user_id"),
                  "amount_cents": payload.get("amount_cents")},
            headers=headers, timeout=HTTP_TIMEOUT,
        )
        if res.status_code == 201:
            return await _advance_saga(client, order_id, "PaymentCharged", f"pay-chg-{msg_id}")
        return await _advance_saga(client, order_id, "PaymentFailed", f"pay-fail-{msg_id}",
                                   {"reason": f"HTTP {res.status_code}"})

    if msg_type == "RefundPaymentCommand":
        # Compensating leg. payment-service treats an uncharged order as a
        # no-op and returns 200, so this is safe to issue unconditionally --
        # which the reaper does, because at INVENTORY_RESERVED it cannot know
        # whether a charge landed before the acknowledgement dropped.
        res = await client.post(
            f"{PAYMENT_URL}/refund",
            json={"order_id": order_id, "user_id": payload.get("user_id"),
                  "reason": payload.get("reason", "SagaCompensation")},
            headers=headers, timeout=HTTP_TIMEOUT,
        )
        if res.status_code == 200:
            return await _advance_saga(client, order_id, "PaymentRefunded", f"pay-ref-{msg_id}")
        logger.error(f"Refund failed: order={order_id} status={res.status_code}")
        return False  # retry: never abandon an outstanding refund

    if msg_type in ("ReleaseInventoryCommand", "CompensateInventoryCommand"):
        return await _advance_saga(client, order_id, "InventoryReleased", f"inv-rel-{msg_id}")

    if msg_type == "ConfirmOrderCommand":
        return await _advance_saga(client, order_id, "OrderCompleted", f"ord-cmp-{msg_id}")

    if msg_type == "SagaTimedOut":
        logger.warning(f"Saga timed out with no compensation required: order={order_id}")
        return True

    logger.error(f"Unknown command type {msg_type} for order={order_id}; settling")
    return True


async def dispatch_loop() -> None:
    logger.info(f"Saga Dispatcher online (poll={POLL_INTERVAL}s, batch={BATCH_SIZE})")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                conn = await asyncpg.connect(ORDER_DB_URL)
                try:
                    while True:
                        async with conn.transaction():
                            # SKIP LOCKED lets replicas run concurrently without
                            # blocking each other or double-claiming.
                            rows = await conn.fetch(
                                """SELECT id, type, payload FROM outbox_messages
                                   WHERE aggregate_type = 'OrderSaga'
                                     AND processed_at IS NULL
                                     AND (type LIKE '%Command' OR type = 'SagaTimedOut')
                                   ORDER BY created_at
                                   LIMIT $1
                                   FOR UPDATE SKIP LOCKED""",
                                BATCH_SIZE,
                            )
                            if not rows:
                                break
                            for row in rows:
                                msg_id, msg_type = row["id"], row["type"]
                                payload = row["payload"]
                                if isinstance(payload, str):
                                    import json
                                    payload = json.loads(payload)

                                # Rule 4: skip anything this consumer already applied.
                                claimed = await conn.fetchval(
                                    """INSERT INTO processed_events (event_id, consumer, event_type)
                                       VALUES ($1, $2, $3)
                                       ON CONFLICT (event_id, consumer) DO NOTHING
                                       RETURNING event_id""",
                                    msg_id, CONSUMER_NAME, msg_type,
                                )
                                if claimed is None:
                                    await conn.execute(
                                        "UPDATE outbox_messages SET processed_at = NOW() WHERE id = $1",
                                        msg_id)
                                    continue

                                _stats["claimed"] += 1
                                settled = await _handle(client, msg_id, msg_type, payload)
                                if settled:
                                    # The outbox row is immutable except for this
                                    # completion marker. Rewriting `type` would make
                                    # Debezium emit a phantom event and destroy the
                                    # original intent.
                                    await conn.execute(
                                        "UPDATE outbox_messages SET processed_at = NOW() WHERE id = $1",
                                        msg_id)
                                else:
                                    # Not settled: release the claim so a later
                                    # tick retries once the saga has advanced.
                                    await conn.execute(
                                        "DELETE FROM processed_events WHERE event_id = $1 AND consumer = $2",
                                        msg_id, CONSUMER_NAME)
                finally:
                    await conn.close()
            except asyncio.CancelledError:
                logger.info("Saga Dispatcher cancelled")
                raise
            except Exception as exc:
                _stats["errors"] += 1
                logger.error(f"Dispatch loop error: {exc}")
            await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def _startup():
    global _dispatch_task
    _dispatch_task = asyncio.create_task(dispatch_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _dispatch_task:
        _dispatch_task.cancel()


@app.get("/health")
async def health():
    return {"status": "ok", "consumer": CONSUMER_NAME}


@app.get("/metrics")
async def metrics():
    return _stats
