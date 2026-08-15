"""
Outbox retention (Architecture Rule 5, Transactional Outbox Retention).

Rule 5 requires *every* service using the outbox pattern to prune acknowledged
records older than 7 days. Only order-saga implemented it, so ~14 other
outbox tables grew without bound -- which slows Debezium's WAL scanning and
eventually causes disk pressure across the whole cluster.

This lives in the shared library rather than being copy-pasted into each
service's main.py, because that copy-paste is exactly how the platform ended
up with fifteen slightly different idempotency implementations.

Usage in a service's main.py:

    from python_common.retention import start_outbox_cleanup
    from .database import engine

    @app.on_event("startup")
    async def _startup():
        app.state.outbox_cleanup = start_outbox_cleanup(engine)

    @app.on_event("shutdown")
    async def _shutdown():
        app.state.outbox_cleanup.cancel()
"""

import asyncio
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
DEFAULT_INTERVAL_SECONDS = 3600


async def prune_outbox_once(engine, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete acknowledged outbox rows older than the retention window.

    Returns the number of rows removed. Rows with a NULL created_at are
    deliberately left alone: they cannot be aged out safely, and their
    presence indicates a producer that inserted without setting the column
    (raw SQL bypassing the SQLAlchemy default). Those should be found and
    fixed rather than silently deleted.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "DELETE FROM outbox_messages "
                "WHERE created_at IS NOT NULL "
                "  AND created_at < NOW() - make_interval(days => :days)"
            ),
            {"days": retention_days},
        )
        return result.rowcount or 0


async def outbox_cleanup_loop(
    engine,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "Starting outbox retention task (interval: %ss, retention: %s days)",
        interval_seconds,
        retention_days,
    )
    while True:
        try:
            removed = await prune_outbox_once(engine, retention_days)
            if removed:
                logger.info("Outbox retention: purged %s records older than %s days",
                            removed, retention_days)
        except asyncio.CancelledError:
            logger.info("Outbox retention task cancelled")
            raise
        except Exception as exc:
            # Never let a transient database error kill the loop; the next
            # tick retries. A permanently failing prune shows up as a growing
            # table plus these logs, not as a silently dead task.
            logger.error("Outbox retention encountered an error: %s", exc)
        await asyncio.sleep(interval_seconds)


def start_outbox_cleanup(engine, **kwargs) -> asyncio.Task:
    """Schedule the retention loop and return its Task so it can be cancelled."""
    return asyncio.create_task(outbox_cleanup_loop(engine, **kwargs))
