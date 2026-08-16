"""
reindex-worker (:8033) — backfill and full reindex of the product read model.

stream-processor keeps `products` current as events arrive. This worker exists
for the cases that leaves behind: an index created after the events were
published, an index lost or recreated, a consumer that was down while a
migration ran. It reads catalog_db and repairs the documents, then idles.

Rule 2: health and metrics only. There is no endpoint that starts a reindex --
a pass runs when the worker starts, and repeats only if REINDEX_INTERVAL_SECONDS
is set. Re-running it is therefore a restart or a Job, which is the shape this
belongs in anyway.

What it will not do is as important as what it does. It writes a partial
document containing only catalog-owned fields, because price and stock live in
the same document under different owners, and it skips any document the index
already holds a newer copy of. Both are explained in reindex_rules.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from elasticsearch import Elasticsearch
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .reindex_rules import (
    DEFAULT_BATCH_SIZE, Cursor, build_document, is_complete, next_cursor,
    should_index,
)

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

CATALOG_DATABASE_URL = os.getenv(
    "CATALOG_DATABASE_URL",
    "postgresql://admin:supersecret@postgres:5432/catalog_db")
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://elasticsearch:9200")
INDEX_NAME = os.getenv("ES_PRODUCT_INDEX", "products")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", DEFAULT_BATCH_SIZE))
# 0 means "once at startup, then idle". A periodic full reindex is a blunt
# instrument and is off unless someone asks for it.
INTERVAL = int(os.getenv("REINDEX_INTERVAL_SECONDS", "0"))

es = Elasticsearch([ES_URL], request_timeout=30, retry_on_timeout=True,
                   max_retries=5)

_stats = {"passes": 0, "scanned": 0, "indexed": 0, "skipped_newer": 0,
          "errors": 0, "running": False}

# Keyset pagination over (created_at, id). OFFSET would skip rows as the table
# grows underneath a long scan, and a backfill that silently misses rows is
# worse than no backfill.
#
# created_at is aliased to updated_at because that is the read model's field
# name; the products table has no updated_at column of its own.
SCAN_SQL = """
SELECT id::text AS product_id,
       name,
       description,
       created_at AS updated_at
FROM products
WHERE ($1::timestamptz IS NULL)
   OR (created_at, id) > ($1::timestamptz, $2::uuid)
ORDER BY created_at, id
LIMIT $3
"""


def indexed_timestamp(product_id: str):
    """The updated_at of the document already in the index, or None."""
    try:
        res = es.get(index=INDEX_NAME, id=product_id,
                     source_includes=["updated_at"], ignore=[404])
        if not res or not res.get("found"):
            return None
        return (res.get("_source") or {}).get("updated_at")
    except Exception as e:
        # Treated as "unknown", which resolves to indexing. A read failure must
        # not quietly turn into a skipped document.
        logger.warning("could not read indexed copy of %s: %s", product_id, e)
        return None


def write_document(product_id: str, doc: dict) -> None:
    """Partial update, so fields owned by other services survive."""
    es.update(index=INDEX_NAME, id=product_id,
              body={"doc": doc, "doc_as_upsert": True})


async def run_pass(pool) -> None:
    cursor = Cursor()
    scanned = indexed = skipped = 0
    _stats["running"] = True
    logger.info("reindex pass starting (batch %s)", BATCH_SIZE)

    try:
        while True:
            async with pool.acquire() as conn:
                rows = [dict(r) for r in await conn.fetch(
                    SCAN_SQL, cursor.updated_at, cursor.product_id, BATCH_SIZE)]

            for row in rows:
                scanned += 1
                product_id = row["product_id"]
                try:
                    if not should_index(row.get("updated_at"),
                                        indexed_timestamp(product_id)):
                        skipped += 1
                        continue
                    write_document(product_id, build_document(row))
                    indexed += 1
                except Exception:
                    _stats["errors"] += 1
                    logger.exception("failed to reindex %s", product_id)

            if is_complete(rows, BATCH_SIZE):
                break
            cursor = next_cursor(rows, cursor)

        _stats["passes"] += 1
        _stats["scanned"] += scanned
        _stats["indexed"] += indexed
        _stats["skipped_newer"] += skipped
        logger.info("reindex pass complete: scanned=%s indexed=%s skipped_newer=%s",
                    scanned, indexed, skipped)
    finally:
        _stats["running"] = False


async def reindex_loop(pool):
    await run_pass(pool)
    if INTERVAL <= 0:
        logger.info("REINDEX_INTERVAL_SECONDS is 0; idling until restarted")
        return
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await run_pass(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["errors"] += 1
            logger.exception("reindex pass failed; will try again next interval")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await asyncpg.create_pool(CATALOG_DATABASE_URL, min_size=1, max_size=5)
    task = asyncio.create_task(reindex_loop(pool))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await pool.close()


app = FastAPI(title="Reindex Worker", lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return {"index": INDEX_NAME, "batch_size": BATCH_SIZE,
            "interval_seconds": INTERVAL, **_stats}
