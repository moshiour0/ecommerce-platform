"""
webhook-handler (:8035) — terminates external PSP webhooks.

Owns no database of its own. It writes into payment-service's schema, which is
a deliberate exception to Rule 1 recorded in §3b: the alternative is a
synchronous HTTP call into payment-service on the webhook path, and a PSP that
gets a timeout retries, which is precisely the storm this component exists to
absorb. Writing the outbox row directly keeps the hot path to one transaction
against one database.

The four layers of §4, in order. Each one is cheaper than the next, and each
protects the one after it:

  1. HMAC-SHA256 over the raw body      (webhook_rules, unit tested)
  2. Redis SET NX, 7-day TTL            fast path for the common retry
  3. Postgres INSERT ON CONFLICT        durable, survives a Redis flush
  4. Outbox emission, same transaction  no synchronous internal HTTP

Everything answers 200 except a request that fails layer 1. A duplicate is a
success from the PSP's point of view -- we have the event -- and returning
anything else asks it to retry something already recorded, which turns a
duplicate into a flood.
"""

import json
import logging
import os
import time

from fastapi import FastAPI, Header, Request, Response
from pythonjsonlogger import jsonlogger
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .webhook_rules import (
    DEDUP_TTL_SECONDS, Admission, dedup_key, verify_signature,
)

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# Rule 8: no hardcoded fallback for a secret the security posture depends on.
# Without this, every forged request would verify against a default that is
# identical in every deployment and committed to the repository.
WEBHOOK_SECRET = os.getenv("PSP_WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise RuntimeError(
        "FATAL: PSP_WEBHOOK_SECRET is not set. Refusing to start: a webhook "
        "handler without a signing secret accepts anything that reaches it.")

DATABASE_URL = os.getenv(
    "PAYMENT_DATABASE_URL",
    "postgresql+asyncpg://admin:supersecret@postgres:5432/payment_ledger_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=5)
async_session = async_sessionmaker(engine, expire_on_commit=False)
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="Webhook Handler")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/webhooks/psp")
async def receive_webhook(
    request: Request,
    signature: str = Header(None, alias="X-Signature"),
):
    # The raw bytes, never a re-serialization of parsed JSON. Re-encoding
    # changes key order and whitespace, and then every honest request fails
    # its signature check while looking like the PSP's fault.
    raw_body = await request.body()

    # ---- Layer 1: signature -------------------------------------------------
    check = verify_signature(WEBHOOK_SECRET, raw_body, signature, int(time.time()))
    if not check.accepted:
        logger.warning("webhook refused: %s (%s)", check.admission.value, check.detail)
        status = 400 if check.admission is Admission.MALFORMED_SIGNATURE else 401
        return Response(
            content=json.dumps({"detail": check.detail}),
            status_code=status, media_type="application/json")

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        # Signed but not JSON: the signature proves the sender, so this is a
        # real fault at the PSP rather than an attack. 400 so it is visible.
        return Response(content=json.dumps({"detail": "body is not JSON"}),
                        status_code=400, media_type="application/json")

    event_id = event.get("id")
    event_type = event.get("type", "unknown")
    if not event_id:
        return Response(content=json.dumps({"detail": "event has no id"}),
                        status_code=400, media_type="application/json")

    # ---- Layer 2: Redis fast path -------------------------------------------
    # A failure here is not fatal. Redis is an optimization over layer 3, and
    # refusing the webhook because the cache is unavailable would hand the PSP
    # a retry storm at the exact moment infrastructure is already unwell.
    try:
        first_time = await redis_client.set(
            dedup_key(event_id), "1", nx=True, ex=DEDUP_TTL_SECONDS)
        if not first_time:
            logger.info("webhook %s already seen (redis)", event_id)
            return {"status": "duplicate", "layer": "redis"}
    except Exception as e:
        logger.warning("redis dedup unavailable, falling through to postgres: %s", e)

    # ---- Layers 3 and 4: durable dedup and outbox, one transaction ----------
    # Together on purpose. If the outbox row were written separately, a crash
    # between the two would leave the event marked as processed with nothing
    # emitted -- and the dedup would then suppress every retry that could have
    # fixed it. That is worse than a duplicate.
    async with async_session() as session:
        result = await session.execute(
            text("""
                INSERT INTO processed_webhooks (event_id, event_type, received_at)
                VALUES (:event_id, :event_type, NOW())
                ON CONFLICT (event_id) DO NOTHING
            """),
            {"event_id": event_id, "event_type": event_type})

        if result.rowcount == 0:
            await session.rollback()
            logger.info("webhook %s already seen (postgres)", event_id)
            return {"status": "duplicate", "layer": "postgres"}

        await session.execute(
            text("""
                INSERT INTO outbox_messages
                    (id, aggregate_type, aggregate_id, type, payload, created_at)
                VALUES (gen_random_uuid(), 'Payment', :aggregate_id,
                        :type, CAST(:payload AS jsonb), NOW())
            """),
            {
                "aggregate_id": str(event.get("payment_intent_id") or event_id),
                "type": f"PspWebhook.{event_type}",
                "payload": json.dumps({
                    "event_id": event_id,
                    "event_type": event_type,
                    "received_at": int(time.time()),
                    "psp_payload": event,
                }),
            })

        try:
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error("webhook %s failed to record: %s", event_id, e)
            # 500 so the PSP retries: nothing was recorded, so a retry is the
            # correct outcome rather than a duplicate.
            return Response(content=json.dumps({"detail": "failed to record"}),
                            status_code=500, media_type="application/json")

    logger.info("webhook %s accepted and emitted", event_id)
    return {"status": "accepted", "event_id": event_id}
