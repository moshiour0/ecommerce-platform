import logging
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import payments, escrow
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

app = FastAPI(title="Payment Service")

# Include routes
app.include_router(payments.router)
app.include_router(escrow.router)

# Instrument FastAPI with OpenTelemetry
# Rule 6: install a real TracerProvider before instrumenting. Without it
# every span is non-recording and Rule 6.4's outbox injection writes nothing.
setup_tracing("payment-service")
# Rule 6.4: inject trace context into every outbox row on insert.
# A mapper listener rather than a helper each caller must remember --
# build_outbox_message was exactly such a helper and went uncalled for
# the entire life of the project.
enable_outbox_trace_injection(OutboxMessage)

FastAPIInstrumentor.instrument_app(app)

@app.on_event("startup")
async def startup_event():
    logger.info("Payment Service starting up")
    # Rule 5: prune acknowledged outbox rows older than 7 days.
    app.state.outbox_cleanup = start_outbox_cleanup(engine)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.on_event("shutdown")
async def shutdown_event():
    task = getattr(app.state, "outbox_cleanup", None)
    if task:
        task.cancel()
