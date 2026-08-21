import logging

from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .routes.reviews import router as reviews_router
from .services.review_rules import (EDIT_WINDOW_DAYS, MAX_RATING, MIN_RATING,
                                    REVIEW_WINDOW_DAYS)

# Rule 6: structured JSON logs from every service.
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)

app = FastAPI(title="Reviews Service")

app.include_router(reviews_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/policy")
def policy():
    """The rules a client needs to render the right thing.

    Exposed so a UI can grey out a closed review window or size its star
    control without hardcoding numbers that then drift from this service's.
    """
    return {
        "min_rating": MIN_RATING,
        "max_rating": MAX_RATING,
        "review_window_days": REVIEW_WINDOW_DAYS,
        "edit_window_days": EDIT_WINDOW_DAYS,
        "verified_purchase_required": True,
    }
