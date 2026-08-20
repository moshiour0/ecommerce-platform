import logging

from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .routes.sellers import router as sellers_router
from .services.seller_rules import CURRENT_CONTRACT_VERSION

# Rule 6: structured JSON logs from every service.
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)

app = FastAPI(title="Seller Service")

app.include_router(sellers_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/contract")
def contract_version():
    """Which commission contract sellers must accept.

    Exposed so a seller-facing client can show the current terms without
    hardcoding a version number that then drifts from this service's.
    """
    return {"current_contract_version": CURRENT_CONTRACT_VERSION}
