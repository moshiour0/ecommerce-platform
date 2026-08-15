import logging
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import notifications
from python_common.retention import start_outbox_cleanup
from .database import engine

# Setup structured JSON logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)

app = FastAPI(title="Notification Service")

# Include routes
app.include_router(notifications.router)

# Instrument FastAPI with OpenTelemetry
FastAPIInstrumentor.instrument_app(app)

@app.on_event("startup")
async def startup_event():
    logger.info("Notification Service starting up")
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
