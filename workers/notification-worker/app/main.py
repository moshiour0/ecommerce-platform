"""
notification-worker (:8034) — owns delivery.

notification-service owns notification state; this worker owns getting the
message out and recording what happened (§3b).

Same three-phase shape as the saga dispatcher, for the same reason spelled out
in migration 006: never hold a database transaction across network I/O.

  1. claim    one statement, stamps a lease, commits immediately
  2. send     no transaction held, provider call happens here
  3. settle   one statement, records the outcome or schedules a retry

A claim is a lease rather than a lock, so a worker that dies mid-send does not
strand its rows: any claim older than the lease window is reclaimable. That
makes delivery at-least-once, which for notifications is the right trade -- a
duplicate email is embarrassing, a silently dropped password reset is a support
ticket.
"""

import asyncio
import logging
import os
import random
import socket
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .notification_rules import (
    MAX_ATTEMPTS, Disposition, classify, is_valid_channel,
)
from .providers import get_provider

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "NOTIFICATION_DATABASE_URL",
    "postgresql://admin:supersecret@postgres:5432/notification_db")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
# Must comfortably exceed the longest a provider call can take, or a slow send
# gets its row reclaimed underneath it and the message goes twice.
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "60"))

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

_stats = {"claimed": 0, "delivered": 0, "retried": 0, "failed": 0, "errors": 0}

# One statement. FOR UPDATE SKIP LOCKED lets several workers claim disjoint
# batches without blocking each other; the claimed_at test is what makes a
# dead worker's rows reclaimable.
CLAIM_SQL = """
WITH claimable AS (
    SELECT id
    FROM notification_records
    WHERE status IN ('PENDING', 'RETRY')
      AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
      AND (claimed_at IS NULL
           OR claimed_at < NOW() - make_interval(secs => $1))
    ORDER BY created_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE notification_records n
SET claimed_at = NOW(), claimed_by = $3, status = 'SENDING'
FROM claimable c
WHERE n.id = c.id
RETURNING n.id, n.user_id, n.channel, n.template_name, n.payload, n.attempts;
"""

SETTLE_DELIVERED_SQL = """
UPDATE notification_records
SET status = 'DELIVERED', delivered_at = NOW(), attempts = attempts + 1,
    claimed_at = NULL, claimed_by = NULL, last_error = NULL
WHERE id = $1
"""

SETTLE_RETRY_SQL = """
UPDATE notification_records
SET status = 'RETRY', attempts = attempts + 1, last_error = $2,
    next_attempt_at = NOW() + make_interval(secs => $3),
    claimed_at = NULL, claimed_by = NULL
WHERE id = $1
"""

SETTLE_FAILED_SQL = """
UPDATE notification_records
SET status = 'FAILED', attempts = attempts + 1, last_error = $2,
    claimed_at = NULL, claimed_by = NULL
WHERE id = $1
"""


async def claim_batch(pool):
    async with pool.acquire() as conn:
        return await conn.fetch(CLAIM_SQL, LEASE_SECONDS, BATCH_SIZE, WORKER_ID)


async def settle(pool, record_id, decision):
    async with pool.acquire() as conn:
        if decision.disposition is Disposition.DELIVERED:
            await conn.execute(SETTLE_DELIVERED_SQL, record_id)
            _stats["delivered"] += 1
        elif decision.disposition is Disposition.RETRY:
            await conn.execute(SETTLE_RETRY_SQL, record_id, decision.detail,
                               decision.delay_seconds)
            _stats["retried"] += 1
        else:
            await conn.execute(SETTLE_FAILED_SQL, record_id, decision.detail)
            _stats["failed"] += 1


async def process_one(pool, row):
    channel = row["channel"]
    attempt = row["attempts"] + 1
    payload = row["payload"] or {}
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)

    if not is_valid_channel(channel):
        # Nothing can deliver this, and no number of retries invents a channel.
        from .notification_rules import DeliveryDecision
        await settle(pool, row["id"], DeliveryDecision(
            Disposition.FAILED, None, f"unknown channel {channel!r}"))
        return

    try:
        result = get_provider(channel).send(
            channel, str(row["user_id"]),
            {**payload, "template_name": row["template_name"]})
    except Exception as e:
        # An adapter that raises is a bug rather than a provider verdict, so it
        # is treated as retryable: the next attempt may hit a fixed adapter, and
        # max_attempts still bounds it.
        logger.exception("provider adapter raised")
        result = "provider_error"
        _stats["errors"] += 1

    decision = classify(result, attempt, random.random)
    await settle(pool, row["id"], decision)

    logger.info("notification %s attempt %s -> %s (%s)",
                row["id"], attempt, decision.disposition.value, decision.detail)


async def worker_loop(pool):
    logger.info("notification-worker %s polling every %ss (batch %s, lease %ss)",
                WORKER_ID, POLL_INTERVAL, BATCH_SIZE, LEASE_SECONDS)
    while True:
        try:
            rows = await claim_batch(pool)
            if rows:
                _stats["claimed"] += len(rows)
                # Sequential rather than gathered: the provider is the
                # bottleneck and firing a whole batch at a rate-limited one is
                # how a backlog becomes a ban.
                for row in rows:
                    await process_one(pool, row)
            else:
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["errors"] += 1
            logger.exception("worker loop error; continuing")
            await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    task = asyncio.create_task(worker_loop(pool))
    app.state.pool = pool
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await pool.close()


app = FastAPI(title="Notification Worker", lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    # Rule 2: workers expose health and metrics only, no business API.
    return {"worker_id": WORKER_ID, "max_attempts": MAX_ATTEMPTS, **_stats}
