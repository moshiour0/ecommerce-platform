import logging
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .routes import search
from .es_client import async_es

# Setup structured JSON logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)

app = FastAPI(title="Search Service")

# Include routes
app.include_router(search.router)

# Instrument FastAPI with OpenTelemetry
FastAPIInstrumentor.instrument_app(app)

@app.on_event("startup")
async def startup_event():
    logger.info("Search Service starting up")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Search Service shutting down, closing Elasticsearch connection")
    await async_es.close()

@app.get("/health")
async def health_check():
    return {"status": "ok"}
