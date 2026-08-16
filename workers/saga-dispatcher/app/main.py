"""
Saga Dispatcher (Architecture section 3b).

Closes the saga command/event loop. order-saga writes a command to its outbox
in the same transaction as the state change; this worker claims that command,
invokes the owning service, and feeds the result back as a state transition.
It translates -- it never decides. All business rules live in the state machine.

Transaction discipline
----------------------
Database transactions are never held across network I/O. Each tick is:

  1. claim   one statement, stamps a lease, commits immediately
  2. work    HTTP calls, no transaction held, bounded concurrency
  3. settle  one statement per command, marks processed or clears the claim

The first version wrapped the whole batch in a single transaction with every
HTTP call inside it -- up to ~200s of open transaction holding row locks and a
pooled connection, blocking VACUUM and pinning WAL that Debezium must scan.

A claim is a lease, not a lock, so a crashed dispatcher does not strand rows;
stale claims are reclaimable. Delivery is at-least-once, which is safe because
every downstream call carries an Idempotency-Key and processed_events dedupes.

Transport
---------
Commands are claimed from the order_db outbox directly. That is the
polling-publisher variant of the outbox pattern: nothing publishes to Kafka
directly (Rule 3 holds) and Debezium still streams the same rows to
OrderSaga.events for every other consumer. Moving the claim loop onto Kafka
is the next step; the invoke/settle logic below is transport agnostic.
"""

import asyncio
import json
import logging
import os
import socket

import asyncpg
import httpx
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from python_common.resilience import (
    AsyncCircuitBreaker,
    BulkheadFullError,
    CircuitOpenError,
)
from python_common.tracing import get_tracer, setup_tracing, start_consumer_span
from .dispatch_rules import (
    plan_item_reservations, reservation_idempotency_key,
    SagaAck, classify_saga_response, route_for, settles,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
if not logger.handlers:
    logger.addHandler(_handler)

# Rule 6: a real TracerProvider must exist before any span is opened.
setup_tracing("saga-dispatcher")
tracer = get_tracer(__name__)

CONSUMER_NAME = "saga-dispatcher"
INSTANCE_ID = f"{CONSUMER_NAME}@{socket.gethostname()}"

ORDER_DB_URL = os.getenv("ORDER_DATABASE_URL", "postgresql://admin:supersecret@postgres:5432/order_db")
SAGA_URL = os.getenv("SAGA_URL", "http://order-saga:8012/orders")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8013/inventory")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8015/payments")

POLL_INTERVAL = float(os.getenv("DISPATCH_POLL_SECONDS", "2"))
BATCH_SIZE = int(os.getenv("DISPATCH_BATCH_SIZE", "20"))
HTTP_TIMEOUT = float(os.getenv("DISPATCH_HTTP_TIMEOUT", "10"))
# A claim older than this is assumed to belong to a dispatcher that died
# mid-flight and may be re-claimed. Must comfortably exceed HTTP_TIMEOUT.
CLAIM_LEASE_SECONDS = int(os.getenv("DISPATCH_CLAIM_LEASE_SECONDS", "120"))

app = FastAPI(title="Saga Dispatcher")

# Rule 11: one breaker + bulkhead per downstream. Sized so a stalled payment
# provider cannot consume the concurrency the inventory path needs.
breakers = {
    "inventory-service": AsyncCircuitBreaker("inventory-service", bulkhead=10),
    "payment-service": AsyncCircuitBreaker("payment-service", bulkhead=10),
    "order-saga": AsyncCircuitBreaker("order-saga", bulkhead=20),
}

_pool: asyncpg.Pool | None = None
_client: httpx.AsyncClient | None = None
_dispatch_task: asyncio.Task | None = None
_stats = {"claimed": 0, "advanced": 0, "deferred": 0, "dead_lettered": 0,
          "short_circuited": 0, "errors": 0, "reclaimed": 0}


# --------------------------------------------------------------------------
# claim / settle -- each is exactly one short transaction
# --------------------------------------------------------------------------

CLAIM_SQL = """
WITH claimable AS (
    SELECT id
    FROM outbox_messages
    WHERE aggregate_type = 'OrderSaga'
      AND processed_at IS NULL
      AND (type LIKE '%Command' OR type = 'SagaTimedOut')
      AND (claimed_at IS NULL
           OR claimed_at < NOW() - make_interval(secs => $2))
    ORDER BY created_at
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE outbox_messages o
SET claimed_at = NOW(), claimed_by = $3
FROM claimable c
WHERE o.id = c.id
RETURNING o.id, o.type, o.payload, o.claimed_by IS DISTINCT FROM $3 AS was_stale;
"""


async def claim_batch(pool: asyncpg.Pool) -> list:
    """One statement, one implicit transaction, locks released on return."""
    return await pool.fetch(CLAIM_SQL, BATCH_SIZE, CLAIM_LEASE_SECONDS, INSTANCE_ID)


async def mark_processed(pool: asyncpg.Pool, msg_id) -> None:
    # The outbox row is immutable apart from this completion marker. Rewriting
    # `type` (as the original prototype did) makes Debezium emit a phantom
    # event and destroys the record of what was originally intended.
    await pool.execute("UPDATE outbox_messages SET processed_at = NOW() WHERE id = $1", msg_id)


async def release_claim(pool: asyncpg.Pool, msg_id) -> None:
    """Hand the command back so the next tick retries it immediately."""
    await pool.execute(
        "UPDATE outbox_messages SET claimed_at = NULL, claimed_by = NULL WHERE id = $1",
        msg_id,
    )


async def record_processed_event(pool: asyncpg.Pool, msg_id, msg_type: str) -> bool:
    """Rule 4. Returns False when this consumer already applied the event."""
    row = await pool.fetchval(
        """INSERT INTO processed_events (event_id, consumer, event_type)
           VALUES ($1, $2, $3)
           ON CONFLICT (event_id, consumer) DO NOTHING
           RETURNING event_id""",
        msg_id, CONSUMER_NAME, msg_type,
    )
    return row is not None


async def forget_processed_event(pool: asyncpg.Pool, msg_id) -> None:
    await pool.execute(
        "DELETE FROM processed_events WHERE event_id = $1 AND consumer = $2",
        msg_id, CONSUMER_NAME,
    )


# --------------------------------------------------------------------------
# downstream invocation -- every call goes through a breaker
# --------------------------------------------------------------------------

async def _post(target: str, url: str, payload: dict, idem_key: str) -> httpx.Response:
    async def _do():
        res = await _client.post(
            url, json=payload,
            headers={"Idempotency-Key": idem_key},
            timeout=HTTP_TIMEOUT,
        )
        # Surface 5xx to the breaker; 4xx is a legitimate answer and is
        # returned to the caller to interpret as a business outcome.
        if res.status_code >= 500:
            res.raise_for_status()
        return res

    return await breakers[target].call(_do)


async def _advance_saga(order_id: str, event_type: str, idem_key: str,
                        payload: dict | None = None) -> bool:
    """Feed a result into the state machine. True when the command is settled.

    The saga distinguishes three outcomes and they must not be collapsed:
      409 -- known event, not applicable yet (out-of-order). Retry later.
      422 -- unknown event type. Never valid; do not retry forever.
      2xx -- applied, or acked as a late duplicate at a terminal state.
    """
    res = await _post("order-saga", f"{SAGA_URL}/{order_id}/events",
                      {"event_type": event_type, "payload": payload or {}}, idem_key)

    # The status semantics live in dispatch_rules so they can be tested without
    # a saga to answer. Settling is the consequential half: a deferred or
    # errored command must stay claimable, a dead-lettered one must not.
    ack = classify_saga_response(res.status_code)
    if ack is SagaAck.APPLIED:
        _stats["advanced"] += 1
        logger.info(f"Saga advanced: order={order_id} event={event_type}")
    elif ack is SagaAck.DEFERRED:
        _stats["deferred"] += 1
        logger.warning(f"Out-of-order, will retry: order={order_id} event={event_type}")
    elif ack is SagaAck.DEAD_LETTERED:
        _stats["dead_lettered"] += 1
        logger.error(f"Non-retryable event, dead-lettering: order={order_id} "
                     f"event={event_type} detail={res.text[:200]}")
    else:
        _stats["errors"] += 1
        logger.error(f"Saga rejected event: order={order_id} event={event_type} "
                     f"status={res.status_code}")
    return settles(ack)


async def handle(msg_id, msg_type: str, payload: dict) -> bool:
    order_id = payload.get("order_id")
    if not order_id:
        logger.error(f"Command {msg_type} ({msg_id}) has no order_id; cannot correlate")
        return True  # unroutable forever — settle rather than spin

    idem = str(msg_id)

    if msg_type == "ReserveInventoryCommand":
        # Every line, not just the first. This read items[0] and reserved that
        # one product, so a three-item order held stock for one and charged for
        # three -- and nothing failed anywhere, because the saga only ever
        # asked about the reservation it made.
        plan = plan_item_reservations(payload.get("items_payload", []))
        if not plan.ok:
            return await _advance_saga(order_id, "InventoryReservationFailed",
                                       f"inv-fail-{msg_id}",
                                       {"reason": plan.error})

        # All-or-nothing. Reserves are separate HTTP calls and cannot share a
        # transaction, so partial success is possible and must be undone:
        # stock held for an order that will never complete is exactly the leak
        # ReleaseInventoryCommand exists to prevent. /release works by order_id
        # and settles whatever is held, so one call cleans up any prefix.
        for reservation in plan.items:
            res = await _post(
                "inventory-service", f"{INVENTORY_URL}/reserve",
                {"product_id": reservation.product_id,
                 "quantity": reservation.quantity,
                 # order_id is what makes the hold releasable at all.
                 "order_id": order_id},
                reservation_idempotency_key(msg_id, reservation.product_id))

            if res.status_code == 200:
                continue

            # 409 out of stock / 404 unknown product are business outcomes with
            # their own saga transition, not transport failures.
            logger.warning(
                f"Reservation failed for order={order_id} "
                f"product={reservation.product_id} status={res.status_code}; "
                f"releasing {len(plan.items)} line(s)")

            rollback = await _post("inventory-service", f"{INVENTORY_URL}/release",
                                   {"order_id": order_id}, f"inv-rb-{msg_id}")
            if rollback.status_code != 200:
                # Do not settle. Reporting the failure now would let the saga
                # roll forward while units from the successful lines stay held,
                # and the release would never be retried.
                logger.error(
                    f"Could not release partial reservation for order={order_id} "
                    f"(status={rollback.status_code}); leaving command unsettled")
                return False

            return await _advance_saga(order_id, "InventoryReservationFailed",
                                       f"inv-fail-{msg_id}",
                                       {"reason": "InsufficientStock",
                                        "product_id": reservation.product_id,
                                        "status_code": res.status_code})

        logger.info(f"Reserved {len(plan.items)} line(s) for order={order_id}")
        return await _advance_saga(order_id, "InventoryReserved", f"inv-res-{msg_id}")

    if msg_type == "ChargePaymentCommand":
        res = await _post("payment-service", f"{PAYMENT_URL}/charge",
                          {"order_id": order_id, "user_id": payload.get("user_id"),
                           "amount_cents": payload.get("amount_cents")}, idem)
        if res.status_code == 201:
            return await _advance_saga(order_id, "PaymentCharged", f"pay-chg-{msg_id}")
        return await _advance_saga(order_id, "PaymentFailed", f"pay-fail-{msg_id}",
                                   {"reason": f"HTTP {res.status_code}"})

    if msg_type == "RefundPaymentCommand":
        # payment-service treats an uncharged order as a no-op and returns 200,
        # so this is safe to issue unconditionally -- which the reaper does,
        # because at INVENTORY_RESERVED it cannot know whether a charge landed
        # before the acknowledgement dropped.
        res = await _post("payment-service", f"{PAYMENT_URL}/refund",
                          {"order_id": order_id, "user_id": payload.get("user_id"),
                           "reason": payload.get("reason", "SagaCompensation")}, idem)
        if res.status_code == 200:
            return await _advance_saga(order_id, "PaymentRefunded", f"pay-ref-{msg_id}")
        logger.error(f"Refund failed: order={order_id} status={res.status_code}")
        return False  # never abandon an outstanding refund

    if msg_type in ("ReleaseInventoryCommand", "CompensateInventoryCommand"):
        # inventory-service treats an order holding nothing as a no-op and
        # returns 200, so this is safe to issue unconditionally -- which the
        # reaper does, because at INVENTORY_RESERVED it cannot know whether the
        # reservation landed before the acknowledgement dropped (§5).
        #
        # This used to advance the saga without calling anyone, so a rollback
        # completed while the units stayed reserved forever.
        res = await _post("inventory-service", f"{INVENTORY_URL}/release",
                          {"order_id": order_id}, idem)
        if res.status_code == 200:
            return await _advance_saga(order_id, "InventoryReleased", f"inv-rel-{msg_id}")
        logger.error(f"Inventory release failed: order={order_id} "
                     f"status={res.status_code}")
        return False  # never abandon an outstanding release

    if msg_type == "ConfirmOrderCommand":
        return await _advance_saga(order_id, "OrderCompleted", f"ord-cmp-{msg_id}")

    if msg_type == "SagaTimedOut":
        logger.warning(f"Saga timed out with no compensation required: order={order_id}")
        return True

    logger.error(f"Unknown command type {msg_type} for order={order_id}; settling")
    return True


def order_id_of(payload: dict) -> str | None:
    return payload.get("order_id")


async def process_one(pool: asyncpg.Pool, row) -> bool:
    """Work phase. No transaction is held here. True when the row settled."""
    msg_id, msg_type = row["id"], row["type"]
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    if row["was_stale"]:
        _stats["reclaimed"] += 1
        logger.warning(f"Reclaimed stale command {msg_type} ({msg_id}) from a previous holder")

    first_time = await record_processed_event(pool, msg_id, msg_type)
    if not first_time:
        await mark_processed(pool, msg_id)
        return True

    _stats["claimed"] += 1
    # Rule 6.4: link this span to the producer's span carried in the payload.
    # A link, not a parent: the producing span ended when the transaction
    # committed, so the two are causally related but not nested. Commands the
    # reaper emits via raw SQL carry no trace context -- they originate in a
    # background sweep with no inbound request -- and start a root span here.
    with start_consumer_span(
        tracer, f"dispatch {msg_type}", payload,
        **{"saga.order_id": order_id_of(payload),
           "saga.command": msg_type,
           "messaging.message_id": str(msg_id)},
    ):
        try:
            settled = await handle(msg_id, msg_type, payload)
        except (CircuitOpenError, BulkheadFullError) as exc:
            # Shed load without consuming a retry budget: the command goes back
            # on the queue untouched and the next tick tries again.
            _stats["short_circuited"] += 1
            logger.warning(f"Shedding {msg_type} ({msg_id}): {exc}")
            settled = False
        except Exception as exc:
            _stats["errors"] += 1
            logger.error(f"Command {msg_type} ({msg_id}) raised: {exc}")
            settled = False

    if settled:
        await mark_processed(pool, msg_id)
    else:
        await forget_processed_event(pool, msg_id)
        await release_claim(pool, msg_id)
    return settled


async def dispatch_loop() -> None:
    logger.info(
        f"Saga Dispatcher online (poll={POLL_INTERVAL}s, batch={BATCH_SIZE}, "
        f"lease={CLAIM_LEASE_SECONDS}s, instance={INSTANCE_ID})"
    )
    while True:
        try:
            rows = await claim_batch(_pool)
            while rows:
                # Concurrent within the batch; per-downstream bulkheads bound
                # how much of that concurrency any one dependency can absorb.
                results = await asyncio.gather(
                    *(process_one(_pool, r) for r in rows), return_exceptions=True)

                # Stop draining when nothing settled. Releasing a claim makes
                # the row instantly re-claimable, so an open circuit would
                # otherwise spin claim -> shed -> release -> claim as fast as
                # Postgres can answer -- measured at ~165 cycles/second against
                # a downed dependency. Falling through to the poll interval
                # paces retries at the rate the circuit can actually recover.
                if not any(r is True for r in results):
                    break
                rows = await claim_batch(_pool)
        except asyncio.CancelledError:
            logger.info("Saga Dispatcher cancelled")
            raise
        except Exception as exc:
            _stats["errors"] += 1
            logger.error(f"Dispatch loop error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def _startup():
    global _pool, _client, _dispatch_task
    _pool = await asyncpg.create_pool(ORDER_DB_URL, min_size=2, max_size=10)
    _client = httpx.AsyncClient()
    # Rule 6.3: propagate the trace over outbound HTTP. Without W3C
    # traceparent headers each downstream call starts a brand new trace and
    # the chain breaks at the very first hop out of this worker.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument_client(_client)
        logger.info("httpx client instrumented for trace propagation")
    except Exception as exc:
        logger.warning(f"httpx instrumentation unavailable: {exc}")
    _dispatch_task = asyncio.create_task(dispatch_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _dispatch_task:
        _dispatch_task.cancel()
    if _client:
        await _client.aclose()
    if _pool:
        await _pool.close()


@app.get("/health")
async def health():
    return {"status": "ok", "consumer": CONSUMER_NAME, "instance": INSTANCE_ID}


@app.get("/metrics")
async def metrics():
    return {**_stats, "breakers": [b.stats() for b in breakers.values()]}
