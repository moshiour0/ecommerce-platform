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
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import asyncpg
from elasticsearch import Elasticsearch
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from python_common.read_model import CATALOG, write_product

from .reindex_rules import (
    CATALOG_TIMESTAMP_FIELD, DEFAULT_BATCH_SIZE, DEFAULT_REAP_GRACE_SECONDS,
    Cursor, build_document, is_complete, is_reapable, next_cursor,
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

# Deleting read-model documents is the one irreversible thing this worker
# can do, so it is off unless asked for. The read model is rebuildable in
# principle, but only for products that still exist in catalog -- which is
# exactly what these documents do not.
REAP_ORPHANS = os.getenv("REAP_ORPHANS", "false").strip().lower() in (
    "1", "true", "yes", "on")
REAP_GRACE_SECONDS = int(os.getenv("REAP_GRACE_SECONDS",
                                   DEFAULT_REAP_GRACE_SECONDS))
# Documents with no timestamp cannot age out of the grace period, so they
# survive every ordinary reap. Separate flag because it removes one of the
# two guards, leaving only the catalog check.
REAP_UNDATED = os.getenv("REAP_UNDATED", "false").strip().lower() in (
    "1", "true", "yes", "on")

es = Elasticsearch([ES_URL], request_timeout=30, retry_on_timeout=True,
                   max_retries=5)

_stats = {"passes": 0, "scanned": 0, "indexed": 0, "skipped_newer": 0,
          "errors": 0, "running": False,
          "reap_examined": 0, "reap_kept": 0, "reaped": 0}

# Keyset pagination over (created_at, id). OFFSET would skip rows as the table
# grows underneath a long scan, and a backfill that silently misses rows is
# worse than no backfill.
#
# created_at is aliased to updated_at because that is the read model's field
# name; the products table has no updated_at column of its own.
SCAN_SQL = """
SELECT id::text AS product_id,
       seller_id::text AS seller_id,
       sku,
       name,
       description,
       is_active,
       -- What kind of thing this is. Indexed so the personalisation term can
       -- ask whether a buyer leans towards this category without a lookup per
       -- result. Listing it in CATALOG_FIELDS was not enough on its own:
       -- build_document only emits fields present on the row, so a field the
       -- scan does not select is silently absent from every backfilled
       -- document and nothing reports it.
       category_id::text AS category_id,
       -- catalog's list price, indexed under its own name so it cannot
       -- collide with the effective price pricing-service publishes.
       price_cents AS base_price_cents,
       created_at AS updated_at
FROM products
WHERE ($1::timestamptz IS NULL)
   OR (created_at, id) > ($1::timestamptz, $2::uuid)
ORDER BY created_at, id
LIMIT $3
"""


def indexed_timestamp(product_id: str):
    """When this worker last wrote the document, or None.

    Deliberately not the document's updated_at: that is bumped by pricing
    and inventory too, so comparing against it makes a product with any
    price history permanently unrepairable by a backfill.
    """
    try:
        res = es.get(index=INDEX_NAME, id=product_id,
                     source_includes=[CATALOG_TIMESTAMP_FIELD], ignore=[404])
        if not res or not res.get("found"):
            return None
        return (res.get("_source") or {}).get(CATALOG_TIMESTAMP_FIELD)
    except Exception as e:
        # Treated as "unknown", which resolves to indexing. A read failure must
        # not quietly turn into a skipped document.
        logger.warning("could not read indexed copy of %s: %s", product_id, e)
        return None


def write_document(product_id: str, doc: dict) -> None:
    """Partial update, so fields owned by other services survive.

    Routed through the shared helper, which refuses anything catalog does not
    own. build_document already filters to CATALOG_FIELDS, so this is a second
    check of the same rule -- deliberately, because the first one is a constant
    in this repository and the second is the platform-wide table.
    """
    write_product(es, product_id, CATALOG, doc, index=INDEX_NAME)


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


ORPHAN_QUERY = {
    "query": {"bool": {"must_not": [{"exists": {"field": "sku"}}]}},
    "_source": ["updated_at", "sku", "catalog_updated_at"],
    "size": 1000,
}


async def reap_pass(pool) -> None:
    """Delete documents that belong to no product.

    Three independent checks before anything is removed, because a document is
    cheap to keep and impossible to recover:

      1. Elasticsearch is asked only for documents with no sku -- the catalog
         marker nothing else writes.
      2. is_reapable re-checks the markers and requires the document to be
         older than the grace period, so a partial document whose
         ProductCreated is merely late is left alone.
      3. catalog_db is asked directly whether a row exists for that id. The
         index having no catalog data and the database having no row are
         different claims, and only the second is authoritative.
    """
    if not REAP_ORPHANS:
        return

    logger.info("reap pass starting (grace %ss)", REAP_GRACE_SECONDS)
    try:
        res = es.search(index=INDEX_NAME, body=ORPHAN_QUERY)
    except Exception:
        _stats["errors"] += 1
        logger.exception("could not search for orphan documents")
        return

    hits = res.get("hits", {}).get("hits", [])
    now = datetime.now(timezone.utc)
    examined = kept = reaped = 0

    for hit in hits:
        examined += 1
        doc_id = hit["_id"]
        decision = is_reapable(hit.get("_source") or {}, now,
                               REAP_GRACE_SECONDS, REAP_UNDATED)
        if not decision.reap:
            kept += 1
            logger.debug("keeping %s: %s", doc_id, decision.reason)
            continue

        # The authoritative check. A document can lack catalog fields for
        # reasons other than the product not existing -- a stripped field, a
        # partial write -- and deleting a real product's document because of
        # one of those would be the worst outcome available here.
        try:
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT 1 FROM products WHERE id = $1::uuid", doc_id)
        except Exception:
            _stats["errors"] += 1
            logger.exception("could not confirm %s against catalog; keeping", doc_id)
            kept += 1
            continue

        if exists:
            kept += 1
            logger.warning(
                "%s has a catalog row but no sku in the index; repairing rather "
                "than reaping", doc_id)
            continue

        try:
            es.delete(index=INDEX_NAME, id=doc_id, ignore=[404])
            reaped += 1
            logger.info("reaped %s: %s", doc_id, decision.reason)
        except Exception:
            _stats["errors"] += 1
            logger.exception("failed to delete %s", doc_id)

    _stats["reap_examined"] += examined
    _stats["reap_kept"] += kept
    _stats["reaped"] += reaped
    logger.info("reap pass complete: examined=%s kept=%s reaped=%s",
                examined, kept, reaped)


async def reindex_loop(pool):
    await run_pass(pool)
    await reap_pass(pool)
    if INTERVAL <= 0:
        logger.info("REINDEX_INTERVAL_SECONDS is 0; idling until restarted")
        return
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await run_pass(pool)
            await reap_pass(pool)
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
            "interval_seconds": INTERVAL, "reap_orphans": REAP_ORPHANS,
            "reap_grace_seconds": REAP_GRACE_SECONDS,
            "reap_undated": REAP_UNDATED, **_stats}
