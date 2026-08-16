import logging

from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .routes.media import router as media_router

# Rule 6: structured JSON logs from every service.
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)

app = FastAPI(title="Media Service")

app.include_router(media_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
