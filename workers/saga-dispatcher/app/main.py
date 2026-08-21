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
import random
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
    Disposition, Outcome, SETTLED, retry, park,
    backoff_seconds, classify_command_failure, disposition_after, MAX_ATTEMPTS,
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
# The escrow ledger is a different router on the same service, and
# PAYMENT_URL already carries the /payments prefix.
PAYMENT_BASE_URL = PAYMENT_URL.rsplit("/payments", 1)[0]

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
          "short_circuited": 0, "errors": 0, "reclaimed": 0,
          # A non-zero "parked" is not a statistic, it is a work queue: each
          # one is an effect the platform intended and could not achieve.
          "parked": 0, "deferred_backoff": 0}


def _from_status(status_code: int, error: str) -> Outcome:
    """Turn a downstream status into a disposition.

    The decision lives in dispatch_rules so it can be tested against every
    status without a downstream to answer. Here it is only applied.
    """
    disposition = classify_command_failure(status_code)
    if disposition is Disposition.SETTLE:
        return SETTLED
    if disposition is Disposition.PARK:
        return park(error)
    return retry(error)


# --------------------------------------------------------------------------
# claim / settle -- each is exactly one short transaction
# --------------------------------------------------------------------------

CLAIM_SQL = """
WITH claimable AS (
    SELECT id
    FROM outbox_messages
    -- SellerOrder as well as OrderSaga. The COD lifecycle emits its stock
    -- movements against the seller order they belong to, and a filter on
    -- 'OrderSaga' alone left them unclaimed forever: every transition applied,
    -- every event was published, and not one unit ever moved.
    WHERE aggregate_type IN ('OrderSaga', 'SellerOrder')
      AND processed_at IS NULL
      -- Parked: tried until the evidence said it never will. Kept on the
      -- table and out of the queue. Without this the row is re-claimed every
      -- tick and, because of the ORDER BY below, ahead of all live work.
      AND parked_at IS NULL
      -- Backing off. This is the clause that ends head-of-line blocking: a
      -- failing row leaves the batch for a growing interval instead of
      -- refilling it, so the commands behind it get their turn.
      AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
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
RETURNING o.id, o.type, o.payload, o.attempts,
          o.claimed_by IS DISTINCT FROM $3 AS was_stale;
"""


async def claim_batch(pool: asyncpg.Pool) -> list:
    """One statement, one implicit transaction, locks released on return."""
    return await pool.fetch(CLAIM_SQL, BATCH_SIZE, CLAIM_LEASE_SECONDS, INSTANCE_ID)


async def mark_processed(pool: asyncpg.Pool, msg_id) -> None:
    # The outbox row is immutable apart from this completion marker. Rewriting
    # `type` (as the original prototype did) makes Debezium emit a phantom
    # event and destroys the record of what was originally intended.
    await pool.execute("UPDATE outbox_messages SET processed_at = NOW() WHERE id = $1", msg_id)


async def defer_claim(pool: asyncpg.Pool, msg_id, delay_seconds: float,
                      error: str | None, count_attempt: bool = True) -> None:
    """Hand the command back, but not before `delay_seconds` have passed.

    This replaced an unconditional release. Releasing made the row instantly
    re-claimable, and since the claim is ordered by created_at the same failing
    rows were re-served first on every tick -- a queue that could not advance
    past its own oldest failure.
    """
    await pool.execute(
        """
        UPDATE outbox_messages
        SET claimed_at = NULL, claimed_by = NULL,
            attempts = attempts + $4,
            next_attempt_at = NOW() + make_interval(secs => $2),
            last_error = $3
        WHERE id = $1
        """,
        msg_id, float(delay_seconds), (error or "")[:500],
        1 if count_attempt else 0,
    )


async def park_claim(pool: asyncpg.Pool, msg_id, error: str | None) -> None:
    """Stop retrying, and keep the row where a person can find it.

    Deliberately not `processed_at`. A processed row is indistinguishable from
    one that succeeded, and the whole point of parking an escrow booking is
    that the money was *not* booked -- recording it as processed would turn a
    visible problem into a silent one.
    """
    await pool.execute(
        """
        UPDATE outbox_messages
        SET claimed_at = NULL, claimed_by = NULL,
            attempts = attempts + 1,
            parked_at = NOW(),
            last_error = $2
        WHERE id = $1
        """,
        msg_id, (error or "")[:500],
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
                        payload: dict | None = None) -> Outcome:
    """Feed a result into the state machine.

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
    # The saga's own vocabulary already separates "not yet" from "never", so
    # this maps onto dispositions rather than re-deciding: DEAD_LETTERED is a
    # settle because the saga has definitively rejected the event and keeping
    # it would be keeping a row nothing can ever act on.
    if settles(ack):
        return SETTLED
    return retry(f"saga answered {res.status_code} to {event_type}")


async def handle(msg_id, msg_type: str, payload: dict) -> Outcome:
    order_id = payload.get("order_id")
    if not order_id:
        logger.error(f"Command {msg_type} ({msg_id}) has no order_id; cannot correlate")
        return SETTLED  # unroutable forever -- settle rather than spin

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
                return retry(f"partial reservation release returned "
                             f"{rollback.status_code}")

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
        # Classified rather than retried blindly. A refund the provider
        # refuses outright is not made to happen by asking again, and asking
        # forever is what pushes every other command out of the batch.
        return _from_status(res.status_code,
                            f"refund returned {res.status_code}")

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
        return _from_status(res.status_code,
                            f"release returned {res.status_code}")

    # Seller-order scoped, and deliberately NOT advancing the parent saga.
    # These belong to one seller's part of a split order; advancing the whole
    # order because one seller's parcel was delivered would drive the buyer's
    # order to a state the other seller has not reached.
    if msg_type in ("ReleaseSellerOrderInventoryCommand",
                    "ConsumeSellerOrderInventoryCommand"):
        endpoint = ("release" if msg_type.startswith("Release") else "consume")
        # Scoped to this seller order's products. Releasing the whole order
        # because one seller cancelled would put another seller's live stock
        # back on sale.
        body = {"order_id": order_id,
                "product_ids": payload.get("product_ids") or None}
        res = await _post("inventory-service", f"{INVENTORY_URL}/{endpoint}",
                          body, idem)
        if res.status_code == 200:
            logger.info(f"{endpoint} for seller order "
                        f"{payload.get('seller_order_id')}: {res.status_code}")
            return SETTLED
        logger.error(f"Seller order inventory {endpoint} failed: "
                     f"order={order_id} status={res.status_code}")
        return _from_status(res.status_code,
                            f"seller order inventory {endpoint} returned "
                            f"{res.status_code}")

    if msg_type == "BookEscrowDeliveryCommand":
        # Seller-order scoped, so it must not advance the parent saga.
        # payment-service is idempotent on (seller_order_id, reason): this is
        # delivered at least once and couriers resend on top of that, and a
        # second booking would credit the seller twice for one parcel.
        res = await _post("payment-service", f"{PAYMENT_BASE_URL}/escrow/delivery",
                          {"seller_id": payload.get("seller_id"),
                           "seller_order_id": payload.get("seller_order_id"),
                           "collected_cents": payload.get("collected_cents"),
                           "currency": payload.get("currency", "BDT")}, idem)
        if res.status_code == 200:
            logger.info(f"escrow booked for seller order "
                        f"{payload.get('seller_order_id')}")
            return SETTLED
        logger.error(f"Escrow booking failed: order={order_id} "
                     f"status={res.status_code} {res.text[:200]}")
        # An unbooked liability is never silently dropped -- but a booking
        # payment-service refuses outright (409: the seller has no commission
        # rate because they do not exist) is not made to succeed by repetition.
        # It parks, loudly, still on the outbox with its payload and its error.
        return _from_status(res.status_code,
                            f"escrow booking returned {res.status_code}: "
                            f"{res.text[:160]}")

    if msg_type == "ConfirmOrderCommand":
        return await _advance_saga(order_id, "OrderCompleted", f"ord-cmp-{msg_id}")

    if msg_type == "SagaTimedOut":
        logger.warning(f"Saga timed out with no compensation required: order={order_id}")
        return SETTLED

    logger.error(f"Unknown command type {msg_type} for order={order_id}; settling")
    return SETTLED


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
        shed = False
        try:
            outcome = await handle(msg_id, msg_type, payload)
        except (CircuitOpenError, BulkheadFullError) as exc:
            # Shed load without consuming a retry budget. A command refused
            # because the platform was busy has told us nothing about whether
            # it can succeed, and letting congestion count toward the attempt
            # cap would park real work for being unlucky.
            _stats["short_circuited"] += 1
            logger.warning(f"Shedding {msg_type} ({msg_id}): {exc}")
            outcome = retry(f"shed: {exc}")
            shed = True
        except Exception as exc:
            _stats["errors"] += 1
            logger.error(f"Command {msg_type} ({msg_id}) raised: {exc}")
            outcome = retry(f"{type(exc).__name__}: {exc}")

    if outcome.settled:
        await mark_processed(pool, msg_id)
        return True

    # Not settled: the effect did not happen, so the idempotency record has to
    # go or the retry would be answered with a cached success.
    await forget_processed_event(pool, msg_id)

    attempts = (row["attempts"] or 0) + (0 if shed else 1)
    disposition = disposition_after(outcome.disposition, attempts)

    if disposition is Disposition.PARK:
        _stats["parked"] += 1
        # Loud, because parking an escrow booking means a seller is owed money
        # the ledger does not know about. This is the line that should page
        # someone; it is not a routine retry.
        logger.error(
            f"PARKED {msg_type} ({msg_id}) after {attempts} attempt(s): "
            f"{outcome.error}. It will not be retried and is not marked "
            f"processed; it stays on the outbox for inspection.")
        await park_claim(pool, msg_id, outcome.error)
        # Parking is progress: the row has left the queue, so the drain loop
        # should carry on and give whatever was stuck behind it a turn.
        return True

    delay = backoff_seconds(max(1, attempts), jitter=random.random())
    _stats["deferred_backoff"] += 1
    logger.warning(
        f"Retrying {msg_type} ({msg_id}) in {delay:.1f}s "
        f"(attempt {attempts}/{MAX_ATTEMPTS}): {outcome.error}")
    await defer_claim(pool, msg_id, delay, outcome.error, count_attempt=not shed)
    return False


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


@app.get("/parked")
async def parked(limit: int = 50):
    """Commands that were tried until the evidence said they never would work.

    This endpoint is the other half of parking. Stopping the retry loop is only
    an improvement if the thing that stopped is visible -- otherwise it is the
    same silent loss as settling, just slower to notice. Each row here is an
    effect the platform intended and did not achieve, with the payload it would
    have sent and the last thing the downstream said about it.

    In-process counters reset when this worker restarts; the table does not,
    which is why the counts are read from Postgres rather than from _stats.
    """
    rows = await _pool.fetch(
        """
        SELECT id, type, aggregate_id, attempts, parked_at, last_error, payload
        FROM outbox_messages
        WHERE parked_at IS NOT NULL
        ORDER BY parked_at DESC
        LIMIT $1
        """, limit)
    total = await _pool.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE parked_at IS NOT NULL")
    backlog = await _pool.fetchval(
        """
        SELECT count(*) FROM outbox_messages
        WHERE processed_at IS NULL AND parked_at IS NULL
          AND (type LIKE '%Command' OR type = 'SagaTimedOut')
        """)
    return {
        "parked_total": total,
        "claimable_backlog": backlog,
        "parked": [
            {"id": str(r["id"]), "type": r["type"],
             "aggregate_id": r["aggregate_id"], "attempts": r["attempts"],
             "parked_at": r["parked_at"].isoformat() if r["parked_at"] else None,
             "last_error": r["last_error"],
             "payload": json.loads(r["payload"]) if isinstance(r["payload"], str)
                        else r["payload"]}
            for r in rows
        ],
    }
