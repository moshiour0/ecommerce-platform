import logging
import asyncio
from sqlalchemy import text
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import orders
from .database import engine
from python_common.tracing import setup_tracing
from python_common.tracing import enable_outbox_trace_injection
from .models import OutboxMessage

# Setup structured JSON logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)

app = FastAPI(title="Order Saga Orchestrator")

# Include routes
app.include_router(orders.router)

# Instrument FastAPI with OpenTelemetry
# Rule 6: install a real TracerProvider before instrumenting. Without it
# every span is non-recording and Rule 6.4's outbox injection writes nothing.
setup_tracing("order-saga")
# Rule 6.4: inject trace context into every outbox row on insert.
# A mapper listener rather than a helper each caller must remember --
# build_outbox_message was exactly such a helper and went uncalled for
# the entire life of the project.
enable_outbox_trace_injection(OutboxMessage)

FastAPIInstrumentor.instrument_app(app)

reaper_task = None
outbox_cleanup_task = None

async def saga_reaper_loop():
    """
    Saga Staleness & Timeout Policy.

    Card sagas stuck in PENDING, INVENTORY_RESERVED or PAID for >15 minutes
    are swept and their compensations emitted via outbox in the same
    transaction.

    COD sagas are swept only at PENDING. Under cash on delivery stock is held
    from checkout until the buyer takes the parcel, which is days rather than
    seconds, so the fifteen-minute window that protects a card order destroys
    a COD one: it would release the stock while the goods are on a van.
    Staleness past that point is a seller or courier problem and belongs to
    the seller order's own timestamps (migration 017), not to a reaper that
    compensates.

    PAID was previously not swept at all. A saga that reached PAID and never
    received OrderCompleted stayed there forever: the card was charged, the
    order never confirmed, and nothing alerted. That is the money-loss hole.

    INVENTORY_RESERVED emits BOTH a release and a refund. At that state we
    cannot tell whether payment succeeded with a lost ack (the classic
    partition case), so we compensate both legs. This requires
    RefundPaymentCommand to be a safe no-op when no charge exists —
    idempotent blind compensation is the standard saga contract.
    """
    logger.info("Starting Saga Reaper background task (interval: 60s, timeout: 15min)")
    while True:
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text("""
                    -- Capture the pre-update status first. UPDATE ... RETURNING
                    -- yields the NEW row, so returning `status` here would give
                    -- 'TIMED_OUT' for every row, no CASE arm would match, and
                    -- unnest(NULL) would emit zero compensating commands --
                    -- silently reaping sagas without compensating them.
                    -- SKIP LOCKED keeps concurrent reaper replicas from blocking.
                    WITH candidates AS (
                        SELECT id, user_id, status
                        FROM order_saga_states
                        WHERE updated_at < NOW() - INTERVAL '15 minutes'
                          AND (
                            -- Card orders complete in seconds, so anything
                            -- still moving after fifteen minutes is stuck.
                            (payment_method = 'CARD'
                             AND status IN ('PENDING', 'INVENTORY_RESERVED', 'PAID'))
                            -- COD orders hold stock from checkout until
                            -- delivery, which is days. Sweeping one at
                            -- INVENTORY_RESERVED would release the stock out
                            -- from under an order already on a van -- and then
                            -- the goods arrive, the buyer pays a courier, and
                            -- nothing in the platform records that anybody is
                            -- owed anything.
                            --
                            -- PENDING is still swept: a reservation that never
                            -- came back in fifteen minutes really is broken,
                            -- whichever way the order is being paid for.
                            OR (payment_method <> 'CARD' AND status = 'PENDING')
                          )
                        FOR UPDATE SKIP LOCKED
                    ),
                    timed_out AS (
                        UPDATE order_saga_states s
                        SET status = 'TIMED_OUT', updated_at = NOW()
                        FROM candidates c
                        WHERE s.id = c.id
                        RETURNING s.id, c.user_id, c.status AS previous_status
                    ),
                    commands AS (
                        SELECT t.id, t.user_id, t.previous_status AS status, c.cmd
                        FROM timed_out t
                        CROSS JOIN LATERAL (
                            SELECT unnest(
                                CASE t.previous_status
                                    WHEN 'PENDING' THEN
                                        ARRAY['SagaTimedOut']
                                    WHEN 'INVENTORY_RESERVED' THEN
                                        ARRAY['ReleaseInventoryCommand', 'RefundPaymentCommand']
                                    WHEN 'PAID' THEN
                                        ARRAY['RefundPaymentCommand']
                                END
                            ) AS cmd
                        ) c
                    )
                    -- id and created_at must be supplied explicitly. Their
                    -- defaults live on the SQLAlchemy model (default=uuid.uuid4),
                    -- which only applies to ORM inserts -- raw SQL gets NULL and
                    -- violates the not-null constraint. A NULL created_at would
                    -- also make the row invisible to the 7-day retention job.
                    INSERT INTO outbox_messages
                        (id, aggregate_type, aggregate_id, type, payload, created_at)
                    SELECT
                        gen_random_uuid(),
                        'OrderSaga',
                        id::text,
                        cmd,
                        json_build_object(
                            'order_id', id::text,
                            'user_id', user_id::text,
                            'previous_status', status,
                            'reason', 'Saga timeout after 15 minutes'
                        ),
                        NOW()
                    FROM commands
                    RETURNING aggregate_id, type;
                """))
                emitted = result.fetchall()
                if emitted:
                    summary = ", ".join(f"{r[0]}:{r[1]}" for r in emitted)
                    logger.warning(
                        f"Saga Reaper swept stuck sagas, emitted {len(emitted)} "
                        f"compensating command(s): {summary}"
                    )
        except Exception as e:
            logger.error(f"Saga Reaper encountered an error: {e}")
        await asyncio.sleep(60)


async def outbox_cleanup_loop():
    """
    E-1 Fix: Outbox Retention Policy.
    Deletes outbox records older than 7 days (Rule 5.4).
    """
    logger.info("Starting Outbox Cleanup background task (interval: 1 hour)")
    while True:
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text("""
                    DELETE FROM outbox_messages 
                    WHERE created_at < NOW() - INTERVAL '7 days'
                """))
                if result.rowcount > 0:
                    logger.info(f"Outbox Cleanup: Purged {result.rowcount} records older than 7 days")
        except Exception as e:
            logger.error(f"Outbox Cleanup encountered an error: {e}")
        await asyncio.sleep(3600)  # Run every hour


@app.on_event("startup")
async def startup_event():
    global reaper_task, outbox_cleanup_task
    logger.info("Order Saga Service starting up")
    reaper_task = asyncio.create_task(saga_reaper_loop())
    outbox_cleanup_task = asyncio.create_task(outbox_cleanup_loop())


@app.on_event("shutdown")
async def shutdown_event():
    global reaper_task, outbox_cleanup_task
    logger.info("Order Saga Service shutting down")
    if reaper_task:
        reaper_task.cancel()
    if outbox_cleanup_task:
        outbox_cleanup_task.cancel()


@app.get("/health")
async def health_check():
    return {"status": "ok"}
