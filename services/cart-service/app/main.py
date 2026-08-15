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

async def cart_sweeper_loop():
    logger.info("Starting Cart Sweeper background task")
    while True:
        try:
            async with engine.begin() as conn:
                # Sweep 1: Expire active carts that have exceeded TTL
                # C-4 Fix: Only sweep status='active', NEVER 'checkout_in_progress'
                await conn.execute(text("""
                    WITH expired AS (
                        UPDATE cart_state 
                        SET status = 'expired' 
                        WHERE expires_at < NOW() AND status = 'active' 
                        RETURNING cart_id, user_id, items
                    ) 
                    INSERT INTO outbox_messages (aggregate_type, aggregate_id, type, payload) 
                    SELECT 'Cart', cart_id, 'CartExpired', json_build_object('user_id', user_id, 'items', items) 
                    FROM expired;
                """))
                
                # Sweep 2: Reclaim stale checkout_in_progress carts (>10 min)
                # If checkout_in_progress for >10 min, something went wrong — reclaim
                await conn.execute(text("""
                    WITH stale_checkout AS (
                        UPDATE cart_state 
                        SET status = 'expired' 
                        WHERE status = 'checkout_in_progress' 
                          AND updated_at < NOW() - INTERVAL '10 minutes'
                        RETURNING cart_id, user_id, items
                    ) 
                    INSERT INTO outbox_messages (aggregate_type, aggregate_id, type, payload) 
                    SELECT 'Cart', cart_id, 'CartExpired', json_build_object('user_id', user_id, 'items', items) 
                    FROM stale_checkout;
                """))
        except Exception as e:
            logger.error(f"Cart Sweeper encountered an error: {e}")
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
