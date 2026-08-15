import logging
import asyncio
from sqlalchemy import text
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import orders
from .database import engine

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
FastAPIInstrumentor.instrument_app(app)

reaper_task = None
outbox_cleanup_task = None

async def saga_reaper_loop():
    """
    S-2 Fix: Saga Staleness & Timeout Policy.
    Sagas stuck in PENDING or INVENTORY_RESERVED for >15 minutes are swept.
    Compensating events are emitted via outbox in the same transaction.
    """
    logger.info("Starting Saga Reaper background task (interval: 60s, timeout: 15min)")
    while True:
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text("""
                    WITH timed_out AS (
                        UPDATE order_saga_states
                        SET status = 'TIMED_OUT', updated_at = NOW()
                        WHERE status IN ('PENDING', 'INVENTORY_RESERVED')
                          AND updated_at < NOW() - INTERVAL '15 minutes'
                        RETURNING id, user_id, status
                    )
                    INSERT INTO outbox_messages (aggregate_type, aggregate_id, type, payload)
                    SELECT 
                        'OrderSaga', 
                        id::text, 
                        CASE 
                            WHEN status = 'INVENTORY_RESERVED' THEN 'ReleaseInventoryCommand'
                            ELSE 'SagaTimedOut'
                        END,
                        json_build_object(
                            'order_id', id::text,
                            'user_id', user_id::text,
                            'previous_status', status,
                            'reason', 'Saga timeout after 15 minutes'
                        )
                    FROM timed_out
                    RETURNING aggregate_id;
                """))
                timed_out_ids = result.fetchall()
                if timed_out_ids:
                    logger.warning(f"Saga Reaper timed out {len(timed_out_ids)} stuck sagas: {[r[0] for r in timed_out_ids]}")
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
