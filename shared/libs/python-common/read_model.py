"""
Field ownership for the shared product read model, and the only supported way
to write it.

Why this exists
---------------
The `products` document in Elasticsearch is assembled from three services, and
this repository has now written the same bug three separate times:

  * tests/e2e/es_healer.py PUT whole documents, stripping sku, updated_at and
    every deactivation on every e2e run;
  * reindex-worker was one line away from writing whole documents built from
    catalog rows, which would have deleted every price and stock level;
  * stream-processor's ProductCreated handler actually did it -- es.index()
    replaces, so a redelivered creation event erased price and stock, and Kafka
    is at-least-once so redelivery is routine.

Each was written by someone who knew the document had several owners. Knowing
was not enough, because the dangerous call (`index`, `PUT`) is shorter to type
than the safe one and looks like what you want. So this module removes the
choice: it exposes one write function, it is always a partial update, and it
refuses fields the calling service does not own.

Ownership
---------
Exactly one service writes each field. The one exception is `updated_at`, which
every writer touches and which therefore means "when anything last changed this
document" -- not "when this owner last changed it". That ambiguity is precisely
why `catalog_updated_at` exists as a separate, single-owner field: reindex-worker
needs to know when *it* last wrote, and comparing against a mark left by pricing
made products with any price history permanently unrepairable.
"""

from typing import Any, Dict, FrozenSet, Iterable, Mapping

PRODUCTS_INDEX = "products"

CATALOG = "catalog-service"
PRICING = "pricing-service"
INVENTORY = "inventory-service"

# Written by every owner, and so owned by none. Kept deliberately small: each
# addition here is a field nobody can reason about.
ANY_OWNER = "*"

PRODUCT_FIELD_OWNERS: Mapping[str, str] = {
    "product_id": CATALOG,
    "sku": CATALOG,
    "name": CATALOG,
    "description": CATALOG,
    "is_active": CATALOG,
    # catalog's list price. Distinct from price_cents on purpose -- one field
    # with two claimants is what made the price ambiguous for the life of the
    # project.
    "base_price_cents": CATALOG,
    # Single-owner timestamp, so a backfill can tell its own writes apart from
    # everyone else's.
    "catalog_updated_at": CATALOG,

    # The effective price a customer pays (§3: pricing owns pricing).
    "price_cents": PRICING,

    "quantity_available": INVENTORY,

    "updated_at": ANY_OWNER,
}


class OwnershipError(ValueError):
    """A service tried to write a field it does not own, or one nobody owns."""


def fields_owned_by(owner: str) -> FrozenSet[str]:
    """Every field `owner` may write, including the shared ones."""
    return frozenset(
        field for field, holder in PRODUCT_FIELD_OWNERS.items()
        if holder == owner or holder == ANY_OWNER
    )


def foreign_fields(owner: str) -> FrozenSet[str]:
    """Fields that belong to somebody else. The ones a bug would erase."""
    return frozenset(
        field for field, holder in PRODUCT_FIELD_OWNERS.items()
        if holder != owner and holder != ANY_OWNER
    )


def validate_product_write(owner: str, fields: Iterable[str]) -> None:
    """Raise unless `owner` may write every one of `fields`.

    Unknown fields are refused as well as foreign ones. Elasticsearch maps new
    fields dynamically, so a typo does not fail -- it silently creates
    `quantity_avaliable` alongside the real column and the document looks fine
    until someone searches on it.
    """
    if owner not in {CATALOG, PRICING, INVENTORY}:
        raise OwnershipError(
            f"unknown writer {owner!r}; expected one of "
            f"{sorted({CATALOG, PRICING, INVENTORY})}")

    allowed = fields_owned_by(owner)
    offending = [f for f in fields if f not in allowed]
    if not offending:
        return

    unknown = [f for f in offending if f not in PRODUCT_FIELD_OWNERS]
    stolen = [f for f in offending if f in PRODUCT_FIELD_OWNERS]

    parts = []
    if stolen:
        parts.append(
            "fields owned by another service: "
            + ", ".join(f"{f} ({PRODUCT_FIELD_OWNERS[f]})" for f in sorted(stolen)))
    if unknown:
        parts.append("fields nobody owns: " + ", ".join(sorted(unknown)))

    raise OwnershipError(
        f"{owner} may not write " + "; ".join(parts)
        + ". Writing another service's field into this document overwrites "
          "live data that this service cannot reconstruct.")


def product_update_body(owner: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """The Elasticsearch body for a partial product write.

    Always `doc_as_upsert`, never a whole document. The upsert half matters for
    ordering: a price arriving before its ProductCreated should keep the price
    rather than be dropped, and it becomes visible once catalog catches up --
    search requires the catalog marker, so a partial document is never served
    as a product.
    """
    validate_product_write(owner, fields.keys())
    return {"doc": dict(fields), "doc_as_upsert": True}


def write_product(es, product_id: str, owner: str, fields: Dict[str, Any],
                  index: str = PRODUCTS_INDEX):
    """The only supported way to write a product document.

    `es` is an Elasticsearch client. Deliberately no counterpart that replaces a
    document: if one existed, it would eventually be used, which is the entire
    history of this file.
    """
    body = product_update_body(owner, fields)
    # product_id is the document id, so it costs nothing to keep it in the
    # source too. Upserts from pricing and inventory used to omit it, leaving
    # documents whose whole content was a stock level and a timestamp.
    body["doc"].setdefault("product_id", product_id)
    return es.update(index=index, id=product_id, body=body)
