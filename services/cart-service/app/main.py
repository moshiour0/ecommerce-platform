import logging
import asyncio
from sqlalchemy import text
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import cart
from .database import redis_client, engine
from python_common.retention import start_outbox_cleanup
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

app = FastAPI(title="Cart Service")

# Include routes
app.include_router(cart.router)

# Instrument FastAPI with OpenTelemetry
# Rule 6: install a real TracerProvider before instrumenting. Without it
# every span is non-recording and Rule 6.4's outbox injection writes nothing.
setup_tracing("cart-service")
# Rule 6.4: inject trace context into every outbox row on insert.
# A mapper listener rather than a helper each caller must remember --
# build_outbox_message was exactly such a helper and went uncalled for
# the entire life of the project.
enable_outbox_trace_injection(OutboxMessage)

FastAPIInstrumentor.instrument_app(app)

sweeper_task = None

# Both sweeps write an outbox row for every cart they expire. id and created_at
# are supplied explicitly: their defaults live on the SQLAlchemy model
# (default=uuid.uuid4) and only apply to ORM inserts, so raw SQL gets NULL and
# violates the not-null constraint. order-saga's dispatcher already carries this
# same comment -- the sweeper is where it had not been applied, and the cost was
# total: the failed INSERT aborted the transaction, rolling back the status
# UPDATE with it, so no cart was ever expired and no CartExpired event was ever
# written.
#
# aggregate_id is the user id, matching the CartCheckoutInitiated event emitted
# by checkout_cart. Debezium keys the Kafka message by this field, so two events
# about the same cart under different keys would land on different partitions
# and lose their ordering relative to each other.
EXPIRE_BY_TTL_SQL = """
    WITH expired AS (
        UPDATE cart_state
        SET status = 'expired', updated_at = NOW()
        WHERE expires_at < NOW() AND status = 'active'
        RETURNING cart_id, user_id, items
    )
    INSERT INTO outbox_messages
        (id, aggregate_type, aggregate_id, type, payload, created_at)
    SELECT gen_random_uuid(), 'Cart', user_id::text, 'CartExpired',
           json_build_object('cart_id', cart_id, 'user_id', user_id,
                             'items', items, 'reason', 'ttl_expired'),
           NOW()
    FROM expired;
"""

# §3: a cart that has sat in checkout_in_progress for more than ten minutes is
# reclaimed. This filtered on cart_state.updated_at, which did not exist until
# migration 010, so the statement failed every minute since it was written and
# 34 carts were stranded in checkout_in_progress -- each unable to be checked
# out again and holding inventory nothing would release.
RECLAIM_STALE_CHECKOUT_SQL = """
    WITH stale_checkout AS (
        UPDATE cart_state
        SET status = 'expired', updated_at = NOW()
        WHERE status = 'checkout_in_progress'
          AND updated_at < NOW() - INTERVAL '10 minutes'
        RETURNING cart_id, user_id, items
    )
    INSERT INTO outbox_messages
        (id, aggregate_type, aggregate_id, type, payload, created_at)
    SELECT gen_random_uuid(), 'Cart', user_id::text, 'CartExpired',
           json_build_object('cart_id', cart_id, 'user_id', user_id,
                             'items', items, 'reason', 'checkout_abandoned'),
           NOW()
    FROM stale_checkout;
"""


async def run_sweep(name: str, sql: str) -> None:
    """One sweep, in its own transaction.

    Separate transactions on purpose. Both sweeps used to share one, so the
    first one's failure rolled back the second as well -- and since the first
    one failed on every run, the reclaim pass never executed even after its own
    bug was the only thing left standing between 34 carts and being released.
    """
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            if result.rowcount:
                logger.info("Cart sweeper: %s expired %s cart(s)", name, result.rowcount)
    except Exception as e:
        logger.error("Cart sweeper: %s failed: %s", name, e)


async def cart_sweeper_loop():
    logger.info("Starting Cart Sweeper background task")
    while True:
        # C-4: only 'active' carts expire by TTL; a checkout in flight is
        # reclaimed by the second sweep on a much longer clock.
        await run_sweep("ttl", EXPIRE_BY_TTL_SQL)
        await run_sweep("stale-checkout", RECLAIM_STALE_CHECKOUT_SQL)
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    global sweeper_task
    logger.info("Cart Service starting up")
    sweeper_task = asyncio.create_task(cart_sweeper_loop())
    # Rule 5: prune acknowledged outbox rows older than 7 days.
    app.state.outbox_cleanup = start_outbox_cleanup(engine)

@app.on_event("shutdown")
async def shutdown_event():
    global sweeper_task
    logger.info("Cart Service shutting down")
    if sweeper_task:
        sweeper_task.cancel()
    await redis_client.close()

@app.get("/health")
async def health_check():
    return {"status": "ok"}
