import asyncio
import logging

from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .database import async_session
from .routes.behaviour import router as behaviour_router
from python_common.affinity_rules import (EVENT_WEIGHTS, HALF_LIFE_DAYS,
                                          MIN_EVENTS_FOR_AFFINITY)
from .services.behaviour_service import sweep_expired

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Personalisation Service")
app.include_router(behaviour_router)

_retention_task = None


async def _retention_loop():
    """Delete behaviour too old to affect a ranking.

    A daily sweep rather than a one-off script, because retention that depends
    on somebody remembering to run it is retention that quietly stops
    happening.
    """
    while True:
        try:
            async with async_session() as db:
                await sweep_expired(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Retention sweep failed: {exc}")
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def _startup():
    global _retention_task
    _retention_task = asyncio.create_task(_retention_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _retention_task:
        _retention_task.cancel()


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/policy")
def policy():
    """What this service does, in numbers.

    Exposed because personalisation decides what people see, and the parameters
    that decide it should not require reading the source.
    """
    return {
        "event_kinds": sorted(EVENT_WEIGHTS),
        "event_weights": EVENT_WEIGHTS,
        "half_life_days": HALF_LIFE_DAYS,
        "min_events_for_affinity": MIN_EVENTS_FOR_AFFINITY,
        "identified_buyers_only": True,
        "buyer_can_read_own_profile": True,
        "buyer_can_delete_own_history": True,
    }
