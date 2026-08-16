import logging

from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .routes.audit import router as audit_router

# Rule 6: structured JSON logs from every service.
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)

app = FastAPI(title="Audit Service")

app.include_router(audit_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
