"""
Reindex decisions, as pure functions.

A backfill rebuilds the `products` read model from the catalog, which sounds
like "write the document" and is not. The document has three owners:

    catalog-service    product_id, sku, name, description, is_active
    pricing-service    price_cents
    inventory-service  quantity_available

stream-processor maintains the last two with partial updates as events arrive.
A reindex that writes a whole document from catalog rows therefore deletes
every price and stock level in the index -- and does it quietly, because the
documents still exist and still look plausible. Search would return products at
no price until the next price change happened to arrive for each one.

So a reindex writes only the fields it owns, as a partial update. The field
list is a constant rather than "whatever the row has", and there is a test that
fails if a foreign field ever appears in it.

The second hazard is time. A backfill runs for minutes while CDC keeps
delivering, so the two write to the same documents concurrently, and the
backfill is reading a snapshot that is already stale. Anything it writes over a
newer incremental update is a regression that lasts until that product changes
again. Hence the freshness guard: a source row is only written when the indexed
copy is not already newer.

That guard is a read-then-write and therefore racy in the small -- an update
landing between the read and the write still loses. Narrowing the window is
worth doing even though closing it here is not possible; closing it properly
needs external versioning on both writers, which is a change to
stream-processor and is not in this component's scope.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Exactly the fields catalog-service owns. Adding price_cents or
# quantity_available here would make a backfill erase live data.
#
# Two of these are aspirational against the current schema. catalog_db.products
# has id, category_id, name, description, price_cents and created_at -- there is
# no sku and no is_active column, even though both appear in the Elasticsearch
# mapping and in the ProductCreated event that stream-processor indexes. They
# reach the read model through the event payload and are never persisted by the
# write model, so a backfill cannot restore them: the source of truth does not
# know them. build_document only emits fields present on the row, so they are
# skipped rather than written as nulls, and they are listed here so that the day
# the columns exist the backfill picks them up without another edit.
#
# catalog_db.products DOES have price_cents, and it is deliberately not indexed
# from here. The read model's price_cents is maintained by pricing-service,
# which owns pricing (§3); catalog's column is a base price that may not be what
# pricing last published. Writing it during a backfill would silently roll every
# product back to its base price.
CATALOG_FIELDS = ("product_id", "sku", "name", "description", "is_active",
                  "updated_at")

# Fields owned by other services. Named explicitly so the guarantee is
# checkable rather than implied by the absence of a name from the list above.
FOREIGN_FIELDS = ("price_cents", "quantity_available")

DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class Cursor:
    """Keyset pagination position: the last row written.

    Keyset rather than OFFSET because a backfill runs while rows are being
    inserted, and OFFSET with a shifting table skips rows silently -- the one
    failure a reindex must not have, since its whole purpose is completeness.
    """

    updated_at: Optional[datetime] = None
    product_id: Optional[str] = None

    @property
    def at_start(self) -> bool:
        return self.updated_at is None and self.product_id is None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort parse of a timestamp from Postgres or Elasticsearch.

    Returns None when the value is absent or unintelligible, which callers
    treat as "unknown" rather than as "old".
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def should_index(source_updated_at: Any, indexed_updated_at: Any) -> bool:
    """Whether a source row should overwrite what is already indexed.

    Skips only when the indexed copy is strictly newer than the source row --
    that is the case where CDC has already delivered something this backfill
    would undo.

    Everything ambiguous resolves to True. An unparseable or missing indexed
    timestamp means the document is absent or damaged, which is exactly what a
    reindex exists to repair, and refusing to write it would make the backfill
    silently incomplete. Equal timestamps are also written: rewriting identical
    data is harmless, while skipping on equality would leave a document that
    was partially written at that instant permanently broken.
    """
    indexed = parse_timestamp(indexed_updated_at)
    if indexed is None:
        return True

    source = parse_timestamp(source_updated_at)
    if source is None:
        # The source row has no usable timestamp, so freshness cannot be
        # compared. Writing keeps the backfill complete; the guard is an
        # optimisation against clobbering, not a correctness gate on its own.
        return True

    return source >= indexed


def build_document(row: Dict[str, Any]) -> Dict[str, Any]:
    """The partial document a reindex writes for one catalog row.

    Only catalog-owned fields, and only those present on the row, so a NULL
    column does not overwrite an indexed value with None.
    """
    doc = {}
    for field in CATALOG_FIELDS:
        if field in row and row[field] is not None:
            value = row[field]
            doc[field] = value.isoformat() if isinstance(value, datetime) else value
    return doc


def next_cursor(rows: List[Dict[str, Any]], current: Cursor) -> Cursor:
    """Advance the cursor past the last row of a batch.

    Returns the current cursor unchanged for an empty batch, so an exhausted
    scan cannot loop forever re-reading nothing.
    """
    if not rows:
        return current
    last = rows[-1]
    return Cursor(updated_at=parse_timestamp(last.get("updated_at")),
                  product_id=str(last.get("product_id"))
                  if last.get("product_id") is not None else None)


def is_complete(rows: List[Dict[str, Any]], batch_size: int) -> bool:
    """A short batch means the scan has reached the end."""
    return len(rows) < batch_size
